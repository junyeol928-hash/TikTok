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


def test_product_rows_carry_evidence(seeded):
    """商品行が『根拠になった動画』とタグを持つこと (UI の主役)."""
    rows = Api(seeded).signals(72, "JP", "product", None, None, 5)["rows"]
    assert rows
    top = rows[0]
    assert top["extra"].get("top_videos"), "代表動画が無い"
    assert top["extra"].get("hashtags"), "タグが無い"
    for v in top["extra"]["top_videos"]:
        assert v.get("views") is not None
