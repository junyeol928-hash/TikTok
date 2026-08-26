"""レスポンス正規化のテスト.

TikTok 側のキー名変更に耐えるための総当たり探索が
実際に機能しているかを確認する。
"""

import pytest

from ttradar.collectors.base import dedupe, find_list, parse_count, pluck, pluck_count
from ttradar.collectors.creative_center import (parse_hashtag, parse_keyword,
                                                parse_product, parse_song,
                                                parse_video)
from ttradar.models import EntityType, M, Snapshot


@pytest.mark.parametrize("raw,expected", [
    (1234, 1234.0),
    ("12,345", 12345.0),
    ("1.2M", 1_200_000.0),
    ("1.5K", 1500.0),
    ("2.1B", 2_100_000_000.0),
    ("3.4万", 34_000.0),          # 日本語表記
    ("1億", 100_000_000.0),
    ("1千", 1000.0),
    ("45%", 0.45),
    ("¥3,980", 3980.0),           # 通貨記号付き
    ("  500  ", 500.0),
    ("-", None), ("", None), (None, None), ("N/A", None), (True, None),
])
def test_parse_count(raw, expected):
    assert parse_count(raw) == expected


def test_pluck_prefers_first_present_key():
    d = {"views": None, "play_count": "2.3M", "stats": {"nested": 7}}
    assert pluck(d, "views", "play_count") == "2.3M"
    assert pluck(d, "stats.nested") == 7
    assert pluck(d, "missing", default="fallback") == "fallback"
    assert pluck_count(d, "views", "play_count") == 2_300_000.0


def test_find_list_uses_hint_then_falls_back():
    hinted = {"code": 0, "data": {"page": 1, "hashtag_list": [{"a": 1}, {"a": 2}]}}
    assert find_list(hinted, "data.hashtag_list") == [{"a": 1}, {"a": 2}]
    # ヒントが外れても再帰探索で最大のリストを見つける
    unknown = {"result": {"payload": {"items_v3": [{"b": 1}, {"b": 2}, {"b": 3}]}}}
    assert len(find_list(unknown, "data.list")) == 3
    assert find_list(None) == []
    assert find_list({"empty": []}) == []


def test_parse_hashtag_normalizes():
    s = parse_hashtag({
        "hashtag_name": "購入品紹介", "publish_cnt": "12.4K",
        "video_views": "3.2M", "rank": 3,
        "industry_info": {"value": "Beauty"},
        "trend": [{"value": 800}, {"value": 1100}],
    }, "JP", "cc")
    assert s.name == "#購入品紹介"
    assert s.metrics[M.POSTS] == 12_400
    assert s.metrics[M.VIEWS] == 3_200_000
    assert s.category == "Beauty"
    assert s.extra["trend_points"] == [800.0, 1100.0]
    assert s.primary_value == 12_400          # ハッシュタグの主要指標は投稿数


def test_parse_product_normalizes_commission_percent():
    """報酬率が 18 (パーセント整数) で来ても 0.18 に正規化されること."""
    s = parse_product({"product_id": "1", "product_name": "テスト商品",
                       "sales": "8,420", "price": "¥1,280",
                       "commission_rate": 18, "video_count": 42}, "JP", "cc")
    assert s.metrics[M.COMMISSION_RATE] == pytest.approx(0.18)
    assert s.metrics[M.PRICE] == 1280
    assert s.metrics[M.SALES] == 8420
    # 既に 0-1 のものは変換しない
    s2 = parse_product({"product_id": "2", "product_name": "x",
                        "commission_rate": 0.25}, "JP", "cc")
    assert s2.metrics[M.COMMISSION_RATE] == pytest.approx(0.25)


def test_parse_video_computes_engagement_rate():
    s = parse_video({"id": "v1", "brand_name": "レビュー",
                     "video_views": "1M", "like": "90K",
                     "comment": "5K", "share": "5K"}, "JP", "cc")
    assert s.metrics[M.ENGAGEMENT_RATE] == pytest.approx(0.1)


def test_parsers_return_none_on_missing_identity():
    assert parse_hashtag({"publish_cnt": 10}, "JP", "cc") is None
    assert parse_song({"video_count": 10}, "JP", "cc") is None
    assert parse_keyword({"search_volume": 10}, "JP", "cc") is None
    assert parse_video({"video_views": 10}, "JP", "cc") is None


def test_dedupe_keeps_richer_snapshot():
    thin = Snapshot(EntityType.HASHTAG, "x", "#x", "a", {M.POSTS: 1})
    rich = Snapshot(EntityType.HASHTAG, "x", "#x", "a", {M.POSTS: 1, M.VIEWS: 2})
    out = dedupe([thin, rich])
    assert len(out) == 1 and len(out[0].metrics) == 2


def test_entity_key_is_stable_and_normalized():
    """表記ゆれ (#付き / 大文字小文字 / 空白) を同一視すること."""
    a = Snapshot(EntityType.HASHTAG, "#Buy", "x", "s1").entity_key
    b = Snapshot(EntityType.HASHTAG, " buy ", "y", "s2").entity_key
    assert a == b
    # 種別が違えば別キー
    c = Snapshot(EntityType.KEYWORD, "buy", "z", "s1").entity_key
    assert a != c
