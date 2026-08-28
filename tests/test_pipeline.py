"""DB・スコアリング・パイプライン全体のテスト."""

import time

import pytest

from ttradar.analysis.digest import Radar
from ttradar.analysis.metrics import compute_growth, classify_stage
from ttradar.analysis.scoring import score_generic, score_product
from ttradar.analysis.rollup import rollup_all
from ttradar.collectors.demo import DemoCollector
from ttradar.config import Config, ProductWeights, ScoreWeights
from ttradar.db import Database
from ttradar.models import EntityType, M, Snapshot, TrendStage


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.sources = ["demo"]
    c.db_path = str(tmp_path / "t.db")
    c.report_dir = str(tmp_path / "rep")
    return c


# ------------------------------------------------------------------------ DB

def test_snapshots_are_append_only_and_deduped(db):
    now = time.time()
    s = Snapshot(EntityType.HASHTAG, "x", "#x", "demo", {M.POSTS: 100}, captured_at=now)
    assert db.upsert_snapshots([s]) == 1
    assert db.upsert_snapshots([s]) == 0        # 同一時刻の重複は無視
    s2 = Snapshot(EntityType.HASHTAG, "x", "#x", "demo", {M.POSTS: 150},
                  captured_at=now + 3600)
    assert db.upsert_snapshots([s2]) == 1
    hist = db.history(s.entity_key)
    assert [h["primary_value"] for h in hist] == [100.0, 150.0]
    assert db.entity_count() == 1               # エンティティは 1 つのまま


def test_first_seen_is_preserved(db):
    now = time.time()
    old = Snapshot(EntityType.HASHTAG, "x", "#x", "demo", {M.POSTS: 1}, captured_at=now - 8000)
    new = Snapshot(EntityType.HASHTAG, "x", "#x", "demo", {M.POSTS: 2}, captured_at=now)
    db.upsert_snapshots([old, new])
    ent = db.active_entities(EntityType.HASHTAG)[0]
    assert ent["first_seen"] == pytest.approx(now - 8000)
    assert ent["last_seen"] == pytest.approx(now)


def test_notification_cooldown(db):
    assert db.was_notified("k", "slack", 24) is False
    db.mark_notified("k", "slack", 90.0)
    assert db.was_notified("k", "slack", 24) is True     # クールダウン中
    assert db.was_notified("k", "slack", 0) is False     # クールダウン 0 なら再通知可
    assert db.was_notified("k", "discord", 24) is False  # チャンネルごとに独立


def test_watchlist_roundtrip(db):
    db.add_watch("creator", "@rival", "競合")
    db.add_watch("creator", "@rival")                    # 重複は無視
    assert len(db.list_watch("creator")) == 1
    db.remove_watch("creator", "@rival")
    assert db.list_watch("creator") == []


def test_prune_removes_old_snapshots(db):
    now = time.time()
    db.upsert_snapshots([
        Snapshot(EntityType.HASHTAG, "old", "#o", "demo", {M.POSTS: 1},
                 captured_at=now - 400 * 86400),
        Snapshot(EntityType.HASHTAG, "new", "#n", "demo", {M.POSTS: 1}, captured_at=now),
    ])
    assert db.snapshot_count() == 2
    db.prune(keep_days=180)
    assert db.snapshot_count() == 1


# ------------------------------------------------------------------ スコアリング

def _growth(values, hours=72):
    now = time.time()
    n = len(values)
    step = hours * 3600 / (n - 1)
    return compute_growth([(now - (n - 1 - i) * step, v) for i, v in enumerate(values)],
                          window_hours=hours, now=now)


def test_good_product_outranks_saturated_one():
    """急伸・高報酬・競合少の商品が、売れているが飽和した商品より上に来ること."""
    comp_cohort = [18, 890]
    sales_cohort = [4200, 22000]
    good_g = _growth([800, 1400, 2600, 4200])
    good, good_why = score_product(
        good_g, classify_stage(good_g),
        {M.SALES: 4200, M.PRICE: 1280, M.COMMISSION_RATE: 0.20,
         M.RELATED_VIDEOS: 18, M.RATING: 4.7},
        sales_cohort, comp_cohort, ProductWeights())

    sat_g = _growth([20000, 21000, 21500, 22000])
    sat, _ = score_product(
        sat_g, classify_stage(sat_g),
        {M.SALES: 22000, M.PRICE: 990, M.COMMISSION_RATE: 0.15,
         M.RELATED_VIDEOS: 890, M.RATING: 4.3},
        sales_cohort, comp_cohort, ProductWeights())

    assert good > sat
    assert any("報酬率が高い" in r for r in good_why)
    assert any("先行者" in r for r in good_why)


def test_declining_product_scores_low():
    g = _growth([9000, 7000, 5000, 3000])
    score, why = score_product(
        g, classify_stage(g),
        {M.SALES: 3000, M.PRICE: 12000, M.COMMISSION_RATE: 0.04,
         M.RELATED_VIDEOS: 400, M.RATING: 3.8},
        [3000], [400], ProductWeights())
    assert score < 35
    assert any("非推奨" in r for r in why)
    assert any("評価が低め" in r for r in why)


def test_scores_are_bounded():
    for values in ([1, 10_000_000], [1000, 1], [5, 5]):
        g = _growth(values)
        s, _ = score_generic(g, classify_stage(g), [1, 100], 1, [1, 100],
                             1.0, ScoreWeights())
        assert 0.0 <= s <= 100.0


def test_reasons_are_always_present():
    g = _growth([100, 200, 400, 800])
    _, why = score_generic(g, classify_stage(g), [800], 5, [5], 2.0, ScoreWeights())
    assert why and all(isinstance(r, str) and r for r in why)


def test_noise_acceleration_does_not_beat_real_growth():
    """ノイズ由来の加速が、本物の急成長より高いスコアにならないこと (退行防止)."""
    noisy = _growth([29638, 29810, 31655, 34363])     # 日次 +5% だが比率 2.5
    real = _growth([8187, 12195, 16470, 25226])       # 日次 +69%
    cohort = [34363, 25226]
    s_noisy, _ = score_generic(noisy, classify_stage(noisy), cohort, 1749,
                               [1749, 500], 5.0, ScoreWeights())
    s_real, _ = score_generic(real, classify_stage(real), cohort, 500,
                              [1749, 500], 5.0, ScoreWeights())
    assert s_real > s_noisy


# -------------------------------------------------------------------- 統合

def _seed(cfg, db, days=7):
    """本番と同じ経路でシードする: 動画を集め、rollup で商品等を導出する."""
    for off in range(days, -1, -1):
        vids = DemoCollector(cfg, day_offset=float(off)).collect("JP")
        db.upsert_snapshots(vids + rollup_all(vids, "JP"))


def test_full_pipeline_with_backfilled_history(cfg):
    """7 日分の履歴を入れて分析まで通ること."""
    db = Database(cfg.db_path)
    _seed(cfg, db)

    digest = Radar(cfg, db).analyze(region="JP", window_hours=72)
    assert digest.total_entities > 0
    assert EntityType.PRODUCT in digest.by_type
    assert EntityType.HASHTAG in digest.by_type

    products = digest.by_type[EntityType.PRODUCT]
    # スコア降順に並んでいること
    assert products == sorted(products, key=lambda s: s.score, reverse=True)
    # 全てのシグナルが根拠を持つこと
    assert all(s.reasons for s in products)
    # 爆発的に伸びる商品が上位に来ること
    top_names = [s.name for s in products[:3]]
    assert any("毛玉取り器" in n or "温感アイマスク" in n for n in top_names)
    # 動画からクリエイター・タグも導出されていること
    assert digest.by_type.get(EntityType.CREATOR)
    assert digest.by_type.get(EntityType.HASHTAG)
    db.close()


def test_analyze_on_empty_db_is_safe(cfg):
    db = Database(cfg.db_path)
    digest = Radar(cfg, db).analyze(region="JP")
    assert digest.total_entities == 0
    assert digest.all_signals() == []
    assert digest.alerts(50) == []
    db.close()


def test_first_run_has_no_growth_but_does_not_crash(cfg):
    """初回実行は履歴が無いので伸び率が出ない. それでも落ちないこと."""
    db = Database(cfg.db_path)
    vids = DemoCollector(cfg, day_offset=0).collect("JP")
    db.upsert_snapshots(vids + rollup_all(vids, "JP"))
    digest = Radar(cfg, db).analyze(region="JP")
    assert digest.total_entities > 0
    assert digest.insufficient_history > 0
    db.close()


def test_collector_failure_does_not_abort_run(cfg):
    """1 つの collector が落ちても他の結果は保存されること."""
    from ttradar.collectors.base import Collector, register

    @register("_boom")
    class Boom(Collector):
        provides = ()
        def collect(self, region):
            raise RuntimeError("意図的な失敗")

    db = Database(cfg.db_path)
    radar = Radar(cfg, db)
    res = radar.collect(sources=["demo", "_boom"])
    assert res.inserted > 0                      # demo の結果は入っている
    assert any("_boom" in e for e in res.errors) # エラーも記録されている
    db.close()


def test_html_report_renders(cfg):
    from ttradar.report.html import build_html
    db = Database(cfg.db_path)
    _seed(cfg, db, days=3)
    html = build_html(Radar(cfg, db).analyze(region="JP", window_hours=48))
    assert "<!doctype html>" in html.lower()
    assert "TikTok トレンドレーダー" in html
    assert "毛玉取り器" in html
    db.close()


def test_niche_matching_boosts_score(cfg):
    cfg.my_niches = ["美容"]
    db = Database(cfg.db_path)
    radar = Radar(cfg, db)
    assert radar._matches_niche("温感アイマスク", "美容") is True
    assert radar._matches_niche("低温調理器", "家電") is False
    db.close()


# ---------------------------------------------------------------- 撮る判断

def _vm(**kw):
    from ttradar.models import M
    base = {M.VIDEO_COUNT: 8.0, M.CREATOR_COUNT: 7.0, M.HIT_RATE: 0.7}
    base.update({getattr(M, k.upper()): v for k, v in kw.items()})
    return base


@pytest.mark.parametrize("stage,metrics,expect", [
    # 下降中は何本あっても遅い
    (TrendStage.DECLINING, _vm(), "late"),
    # 埋もれる
    (TrendStage.RISING, _vm(video_count=200.0), "crowded"),
    # 本数の割に投稿者が少ない = 一人が量産しているだけ
    (TrendStage.RISING, _vm(video_count=9.0, creator_count=2.0), "fake"),
    # まだ誰も試していない
    (TrendStage.RISING, _vm(video_count=2.0, creator_count=2.0), "untested"),
    # 鈍り始め
    (TrendStage.PEAKING, _vm(), "hurry"),
    # 適正範囲を超えている
    (TrendStage.RISING, _vm(video_count=46.0, creator_count=30.0), "compete"),
    # まぐれ 1 本
    (TrendStage.RISING, _vm(hit_rate=0.2), "risky"),
    # 狙い目
    (TrendStage.EMERGING, _vm(), "go"),
    (TrendStage.NEW, _vm(), "go"),
    # 横ばいで特徴なし
    (TrendStage.STABLE, _vm(), "ok"),
])
def test_filming_verdict(stage, metrics, expect):
    """「で、これで撮るの?」に一言で答えられること.

    スコアは順位付けの連続値で、行動を決められない。
    競合の本数・再現性・段階から、迷わない判定を出す。
    """
    from ttradar.analysis.scoring import filming_verdict
    code, label, note = filming_verdict(stage, metrics)
    assert code == expect
    assert label and note, "ラベルと一言は必ず埋まっていること"


def test_verdict_order_matters():
    """下降中は本数が少なくても『先行のチャンス』にしない."""
    from ttradar.analysis.scoring import filming_verdict
    code, _, _ = filming_verdict(TrendStage.DECLINING, _vm(video_count=1.0))
    assert code == "late"


def test_verdict_reaches_signals(tmp_path):
    """商品シグナルに判定が乗ること (UI がそのまま出せる)."""
    from ttradar.analysis.rollup import rollup_all
    from ttradar.collectors.demo import DemoCollector
    from ttradar.config import Config
    from ttradar.db import Database
    from ttradar.analysis.digest import Radar
    from ttradar.models import EntityType

    cfg = Config()
    cfg.sources = ["demo"]
    cfg.db_path = str(tmp_path / "v.db")
    with Database(cfg.db_path) as db:
        for off in range(3, -1, -1):
            v = DemoCollector(cfg, day_offset=float(off)).collect("JP")
            db.upsert_snapshots(v + rollup_all(v, "JP"))
        sigs = Radar(cfg, db).analyze("JP", 72).by_type[EntityType.PRODUCT]

    assert sigs and all(s.verdict for s in sigs), "商品に判定が付いていない"
    # 種類が 1 つに潰れていないこと (全部「撮れる」では判断材料にならない)
    assert len({s.verdict[0] for s in sigs}) >= 3
    d = sigs[0].to_dict()
    assert set(d["verdict"]) == {"code", "label", "note"}
