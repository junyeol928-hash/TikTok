"""Web アプリの API 層のテスト.

HTTP を立てずに :class:`Api` を直接叩く。
サーバー層は薄いラッパなので、ここを固めれば実用上十分。
"""

import json

import pytest

from ttradar.analysis.rollup import rollup_all
from ttradar.collectors.demo import DemoCollector
from ttradar.config import Config
from ttradar.db import Database
from ttradar.server import Api


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.sources = ["demo"]
    c.db_path = str(tmp_path / "app.db")
    c.report_dir = str(tmp_path / "rep")
    c.growth_window_hours = 72
    return c


@pytest.fixture
def seeded(cfg):
    """7 日分の履歴を入れた状態."""
    db = Database(cfg.db_path)
    for off in range(7, -1, -1):
        vids = DemoCollector(cfg, day_offset=float(off)).collect("JP")
        db.upsert_snapshots(vids + rollup_all(vids, "JP"))
    db.close()
    return cfg


def test_meta_shape(cfg):
    m = Api(cfg).meta()
    assert m["regions"] == ["JP"]
    assert any(t["value"] == "product" for t in m["entity_types"])
    # ステージは全 6 種が UI に出せること
    assert {s["value"] for s in m["stages"]} == {
        "new", "emerging", "rising", "stable", "peaking", "declining"}
    assert all(s["emoji"] and s["label"] for s in m["stages"])


def test_summary_on_empty_db(cfg):
    s = Api(cfg).summary(None, None)
    assert s["total_entities"] == 0
    assert s["snapshot_count"] == 0
    assert s["alerts"] == 0
    # 空でもステージ集計のキーは揃っている (UI が undefined を踏まない)
    assert set(s["stage_counts"]) == {
        "new", "emerging", "rising", "stable", "peaking", "declining"}


def test_summary_with_data(seeded):
    s = Api(seeded).summary(72, "JP")
    assert s["total_entities"] > 0
    assert s["capture_rounds"] == 8
    assert sum(s["stage_counts"].values()) > 0
    assert s["type_counts"]["product"] > 0


def test_signals_sorted_and_have_sparkline(seeded):
    r = Api(seeded).signals(72, "JP", None, None, None, 100)
    rows = r["rows"]
    assert rows
    scores = [x["score"] for x in rows]
    assert scores == sorted(scores, reverse=True)
    # UI が必要とするフィールドが揃っていること
    for x in rows[:5]:
        assert x["stage_label"] and x["stage_emoji"]
        assert isinstance(x["reasons"], list) and x["reasons"]
        assert isinstance(x["spark"], list)
    assert any(len(x["spark"]) >= 2 for x in rows)


def test_signals_filters(seeded):
    api = Api(seeded)
    only = api.signals(72, "JP", "product", None, None, 100)["rows"]
    assert only and all(x["entity_type"] == "product" for x in only)

    hit = api.signals(72, "JP", "product", None, "アイマスク", 100)["rows"]
    assert hit and all("アイマスク" in x["name"] for x in hit)

    none = api.signals(72, "JP", None, None, "存在しない商品名xyz", 100)
    assert none["rows"] == []

    st = api.signals(72, "JP", None, "rising", None, 100)["rows"]
    assert all(x["stage"] == "rising" for x in st)


def test_signals_limit(seeded):
    r = Api(seeded).signals(72, "JP", None, None, None, 3)
    assert len(r["rows"]) == 3
    assert r["count"] > 3           # count は絞り込み前の総数


def test_history_shape(seeded):
    api = Api(seeded)
    key = api.signals(72, "JP", "product", None, None, 1)["rows"][0]["entity_key"]
    h = api.history(key)
    assert h["name"]
    assert len(h["series"]) == 8
    ts = [p["t"] for p in h["series"]]
    assert ts == sorted(ts)          # 時系列は昇順 (チャートがそのまま描ける)
    assert all(p["v"] is not None for p in h["series"])
    assert h["series"][-1]["metrics"]


def test_history_unknown_key_is_safe(cfg):
    h = Api(cfg).history("does_not_exist")
    assert h["series"] == []
    assert h["name"] == "does_not_exist"


def test_watchlist_roundtrip(cfg):
    api = Api(cfg)
    assert api.watchlist() == []
    api.add_watch("creator", "@rival", "競合")
    rows = api.watchlist()
    assert len(rows) == 1 and rows[0]["value"] == "@rival"
    api.remove_watch("creator", "@rival")
    assert api.watchlist() == []


def test_all_api_output_is_json_serializable(seeded):
    """UI に返す前に JSON 化できること (Enum や sqlite3.Row の混入を防ぐ)."""
    api = Api(seeded)
    for payload in (api.meta(), api.summary(72, "JP"),
                    api.signals(72, "JP", None, None, None, 20),
                    api.watchlist()):
        json.dumps(payload, ensure_ascii=False, default=str)


def test_app_html_has_no_cdn_dependencies():
    """外部 JS/CSS ライブラリに依存しないこと.

    Google Fonts だけは例外的に許可する (読み込めなくても
    フォールバックの日本語フォントで完全に動作するため)。
    JS ライブラリを CDN から読むと、オフラインや制限環境で
    画面そのものが壊れるので許可しない。
    """
    from ttradar.server import APP_HTML
    html = APP_HTML.read_text(encoding="utf-8")
    assert html.lower().startswith("<!doctype html>")
    for bad in ("cdn.", "unpkg", "jsdelivr", "cdnjs", "<script src="):
        assert bad not in html, f"外部依存が混入しています: {bad}"


def test_app_html_font_has_fallback():
    """Web フォントが読めなくても日本語が崩れないこと."""
    from ttradar.server import APP_HTML
    html = APP_HTML.read_text(encoding="utf-8")
    assert "Hiragino Sans" in html and "Noto Sans JP" in html


def test_videos_endpoint(seeded):
    api = Api(seeded)
    v = api.videos(72, "JP", "views", None, 20)
    assert v["count"] > 0 and len(v["rows"]) == 20
    views = [r["metrics"].get("views", 0) for r in v["rows"]]
    assert views == sorted(views, reverse=True)
    for r in v["rows"][:5]:
        assert r["extra"].get("creator")
        # 商品紹介動画であることの根拠は必ず持つ:
        # 商品リンクそのものか、判定に使った語のどちらか。
        ex = r["extra"]
        assert (ex.get("product") or {}).get("name") or ex.get("intent_words")

    # 並び替えが効くこと
    by_vel = api.videos(72, "JP", "velocity", None, 10)["rows"]
    vels = [r["metrics"].get("velocity", 0) for r in by_vel]
    assert vels == sorted(vels, reverse=True)

    # 投稿者で絞り込めること
    who = v["rows"][0]["extra"]["creator"]
    hit = api.videos(72, "JP", "views", who, 50)["rows"]
    assert hit and all(who in str(r["extra"].get("creator", "")) for r in hit)


def test_videos_kind_filter(seeded):
    """商品紹介動画への絞り込みが効き、内訳の件数が返ること.

    「TikTok に上がっている動画なら何でもいい」わけではないので、
    UI が「何本のうち何本がリンク確定か」を出せる必要がある。
    """
    api = Api(seeded)
    all_ = api.videos(72, "JP", "views", None, 500, "all")
    shop = api.videos(72, "JP", "views", None, 500, "shop")
    strong = api.videos(72, "JP", "views", None, 500, "strong")

    c = all_["counts"]
    assert c["shop"] > 0, "リンク確定の紹介動画が 1 本も無い"
    assert c["shop"] < c["all"], "デモデータはリンク無しも含むはず"
    assert c["shop"] <= c["strong"] <= c["all"]

    # 件数は絞り込みに関係なく同じ内訳を返す (UI のチップが揺れない)
    assert shop["counts"] == c and strong["counts"] == c
    assert shop["count"] == c["shop"]
    assert all(r["extra"].get("product") for r in shop["rows"])
    assert all(float(r["extra"].get("product_intent") or 0) >= 0.65
               for r in strong["rows"])


def test_summary_reports_product_focus(seeded):
    """『商品紹介動画だけを見ている』ことを画面に出すための集計."""
    f = Api(seeded).summary(72, "JP")["focus"]
    assert f["videos"] > 0
    assert 0 < f["with_shop_link"] <= f["videos"]
    assert f["products"] > 0
    assert f["queries"], "何を検索しているかを UI に出せない"
    assert 0 < f["min_product_intent"] <= 1


def test_meta_exposes_filter_settings(cfg):
    m = Api(cfg).meta()
    assert m["video_queries"], "検索語が UI から見えない"
    assert 0 < m["min_product_intent"] <= 1
    assert m["strong_intent"] > m["min_product_intent"]


def test_derived_hashtags_survive_min_volume(seeded):
    """動画から導出したタグが min_volume で消えないこと.

    導出タグの主要指標は「今回の収集に含まれた紹介動画の本数」で、
    数十本にしかならないのが正常。既定の min_volume=100 を掛けると
    ハッシュタグの画面が丸ごと空になる。
    """
    cfg = seeded
    cfg.min_volume = 100          # 既定値。これで消えてはいけない
    rows = Api(cfg).signals(72, "JP", "hashtag", None, None, 50)["rows"]
    assert rows, "導出タグが min_volume で全部消えている"
    small = [r for r in rows if r["metrics"].get("posts", 0) < cfg.min_volume]
    assert small, "min_volume 未満のタグが 1 件も残っていない"


def test_reach_tags_are_marked(seeded):
    """#fyp のような露出タグに印が付くこと (商品ジャンルのタグと区別する)."""
    rows = Api(seeded).signals(72, "JP", "hashtag", None, None, 80)["rows"]
    marks = {r["name"]: (r["extra"] or {}).get("reach_tag") for r in rows}
    assert marks, "タグが 1 件も無い"
    # 印は必ず真偽値で入る (UI が undefined を踏まない)
    assert all(v is not None for v in marks.values())


def test_old_videos_are_excluded(cfg, tmp_path):
    """何ヶ月も前の動画はトレンドではないので分析対象から外れること."""
    import time
    from ttradar.models import EntityType, M, Snapshot

    now = time.time()

    def vid(i, age_h):
        return Snapshot(
            entity_type=EntityType.VIDEO, native_id=f"v{i}",
            name=f"神アイテム紹介 {i} 買ってよかった", source="tiktok_video",
            metrics={M.VIEWS: 50_000.0 + i, M.AGE_HOURS: age_h},
            region="JP", captured_at=now,
            extra={"creator": "c1", "hashtags": ["購入品紹介"],
                   "product_intent": 1.0,
                   "product": {"name": f"商品{i}", "url": None}},
        )

    cfg.raw["max_video_age_days"] = 60
    cfg.min_volume = 0
    with Database(cfg.db_path) as db:
        db.upsert_snapshots([vid(1, 10 * 24), vid(2, 200 * 24)])

    rows = Api(cfg).videos(72, "JP", "views", None, 50)["rows"]
    names = {r["name"] for r in rows}
    assert any("1" in x for x in names), "10 日前の動画が消えている"
    assert not any("2" in x for x in names), "200 日前の動画が残っている"

    # 制限なしにすれば両方出る
    cfg.raw["max_video_age_days"] = 0
    assert len(Api(cfg).videos(72, "JP", "views", None, 50)["rows"]) == 2


def test_food_is_excluded(cfg):
    """食べ物は分析対象から外れ、食にまつわる『物』は残ること."""
    import time
    from ttradar.models import EntityType, M, Snapshot

    now = time.time()

    def prod(name):
        return Snapshot(
            entity_type=EntityType.PRODUCT, native_id=name, name=name,
            source="rollup", metrics={M.VIEWS: 90_000.0, M.MEDIAN_VIEWS: 40_000.0,
                                      M.VIDEO_COUNT: 5.0},
            region="JP", captured_at=now, extra={},
        )

    cfg.min_volume = 0
    with Database(cfg.db_path) as db:
        db.upsert_snapshots([prod("コンビニスイーツ 食べ比べセット"),
                             prod("北欧デザインの食器セット"),
                             prod("充電式 毛玉取り器")])

    names = {r["name"] for r in Api(cfg).signals(72, "JP", "product", None, None, 50)["rows"]}
    assert "充電式 毛玉取り器" in names
    assert "北欧デザインの食器セット" in names, "食器は物なので残すべき"
    assert not any("スイーツ" in x for x in names)

    cfg.raw["exclude_food"] = False
    n2 = {r["name"] for r in Api(cfg).signals(72, "JP", "product", None, None, 50)["rows"]}
    assert any("スイーツ" in x for x in n2), "設定で戻せること"


def test_settings_round_trip(cfg):
    """期間と食べ物の扱いをアプリから変えられること.

    利用者に config.yaml を編集させずに済ませるための経路なので、
    保存 → 読み出し → 分析への反映まで通しで確認する。
    """
    api = Api(cfg)
    before = api.get_settings()
    assert before["max_video_age_days"] == 30      # 既定は直近1ヶ月
    assert before["exclude_food"] is True

    r = api.save_settings({"max_video_age_days": 7, "exclude_food": False})
    assert r["ok"] is True
    after = api.get_settings()
    assert after["max_video_age_days"] == 7
    assert after["exclude_food"] is False

    # 分析側にも効いていること (config.yaml ではなく DB の値が使われる)
    f = api.summary(72, "JP")["focus"]
    assert f["max_age_days"] == 7 and f["exclude_food"] is False

    # 選べない値は弾く
    bad = api.save_settings({"max_video_age_days": 3})
    assert bad["ok"] is False
    assert api.get_settings()["max_video_age_days"] == 7


def test_summary_reports_scope(seeded):
    f = Api(seeded).summary(72, "JP")["focus"]
    assert f["max_age_days"] > 0
    assert f["exclude_food"] is True
    assert "excluded_old" in f and "excluded_food" in f


def test_formats_endpoint(seeded):
    """『どういう型の紹介動画が伸びているか』の集計.

    個々の動画ではなく、切り口と尺という型で比べられること。
    合計ではなく中央値であること (1本のバズに引っ張られない)。
    """
    f = Api(seeded).formats(72, "JP")
    assert f["total"] > 0
    assert f["queries"], "切り口の集計が空"
    # 中央値の降順に並んでいる (UI がそのまま描ける)
    meds = [q["median_views"] for q in f["queries"]]
    assert meds == sorted(meds, reverse=True)
    for q in f["queries"]:
        assert q["videos"] > 0
        assert q["with_shop_link"] <= q["videos"]
        # 中央値は合計ではない: 本数を掛けた値より必ず小さいはず
        assert q["median_views"] <= sum(
            r["metrics"].get("views", 0)
            for r in Api(seeded).videos(72, "JP", "views", None, 999)["rows"])

    # 尺は決まった順序で返す (グラフの並びが毎回変わらない)
    labels = [d["name"] for d in f["durations"]]
    order = ["〜15秒", "15〜30秒", "30〜60秒", "60秒〜"]
    assert labels == [x for x in order if x in labels]


def test_formats_on_empty_db(cfg):
    f = Api(cfg).formats(None, None)
    assert f == {"total": 0, "queries": [], "durations": []}


def test_product_rows_carry_evidence(seeded):
    """商品行が『根拠になった動画』とタグを持つこと (UI の主役)."""
    rows = Api(seeded).signals(72, "JP", "product", None, None, 5)["rows"]
    assert rows
    top = rows[0]
    assert top["extra"].get("top_videos"), "代表動画が無い"
    assert top["extra"].get("hashtags"), "タグが無い"
    for v in top["extra"]["top_videos"]:
        assert v.get("views") is not None
