"""Web アプリの API 層のテスト.

HTTP を立てずに :class:`Api` を直接叩く。
サーバー層は薄いラッパなので、ここを固めれば実用上十分。
"""

import json

import pytest

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
        db.upsert_snapshots(DemoCollector(cfg, day_offset=float(off)).collect("JP"))
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

    hit = api.signals(72, "JP", None, None, "アイマスク", 100)["rows"]
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


def test_app_html_is_self_contained():
    """CDN 依存が無いこと (オフラインで動く要件)."""
    from ttradar.server import APP_HTML
    html = APP_HTML.read_text(encoding="utf-8")
    assert html.lower().startswith("<!doctype html>")
    for bad in ("cdn.", "unpkg", "jsdelivr", "googleapis", "cdnjs"):
        assert bad not in html, f"外部依存が混入しています: {bad}"
