"""TikTok 動画の解析と、動画 → 商品の導出のテスト.

このシステムの中心的な変換なので、実際のレスポンス形状を模したデータで固定する。
"""

import time

import pytest

from ttradar.analysis.rollup import (is_valid_product, normalize_product_name,
                                     product_key, rollup_all, rollup_hashtags,
                                     rollup_products)
from ttradar.analysis.scoring import competition_fit
from ttradar.collectors.tiktok_video import (extract_hashtags,
                                             extract_product_anchor,
                                             find_video_items, parse_item,
                                             product_intent_score)
from ttradar.models import EntityType, M, Snapshot

NOW = int(time.time())


def item(vid="7412345", desc="【購入品紹介】毛玉取り器が神すぎた #購入品紹介 #便利グッズ",
         views=812000, likes=94000, saves=31000, anchor=True, age_h=30):
    d = {
        "id": vid, "desc": desc, "createTime": NOW - int(age_h * 3600),
        "author": {"uniqueId": "reviewer_jp", "nickname": "レビュー太郎",
                   "followerCount": 48000},
        "stats": {"playCount": views, "diggCount": likes, "commentCount": 2100,
                  "shareCount": 5400, "collectCount": saves},
        "video": {"duration": 34, "cover": "https://p16.tiktokcdn.com/x.jpg"},
        "textExtra": [{"hashtagName": "購入品紹介"}, {"hashtagName": "便利グッズ"}],
    }
    if anchor:
        d["anchors"] = [{"type": 2, "keyword": "充電式 毛玉取り器",
                         "schema": "https://shop.tiktok.com/view/product/123"}]
    return d


# ---------------------------------------------------------------- 動画の解析

def test_parse_item_core_metrics():
    s = parse_item(item(), "JP", "tiktok_video")
    assert s.entity_type is EntityType.VIDEO
    assert s.metrics[M.VIEWS] == 812000
    assert s.metrics[M.SAVES] == 31000
    assert s.metrics[M.SAVE_RATE] == pytest.approx(31000 / 812000)
    # 経過時間で正規化した「時速」— 新しい動画と古い動画を公平に比べるため
    assert s.metrics[M.VELOCITY] == pytest.approx(812000 / 30, rel=0.02)
    assert s.url == "https://www.tiktok.com/@reviewer_jp/video/7412345"
    assert s.extra["creator"] == "reviewer_jp"


def test_engagement_includes_saves():
    s = parse_item(item(views=1000, likes=90, saves=50), "JP", "x")
    # いいね+コメント+シェア+保存 を再生数で割る
    expected = (90 + 2100 + 5400 + 50) / 1000
    assert s.metrics[M.ENGAGEMENT_RATE] == pytest.approx(expected)


def test_duration_milliseconds_normalized():
    d = item(); d["video"]["duration"] = 34000       # ミリ秒で来るケース
    s = parse_item(d, "JP", "x")
    assert s.metrics[M.DURATION] == pytest.approx(34.0)


def test_parse_item_without_stats_is_skipped():
    assert parse_item({"id": "1", "desc": "x"}, "JP", "x") is None
    assert parse_item({"stats": {"playCount": 5}}, "JP", "x") is None   # id 無し


def test_find_video_items_by_shape_not_key_name():
    """キー名ではなく『id と stats を持つ』形で判定するので構造変化に強い."""
    it = item()
    payloads = [
        {"itemList": [it]},
        {"data": [{"item": it}]},
        {"weird": {"nested": {"deep": {"item_list_v9": [it]}}}},
    ]
    for p in payloads:
        assert len(find_video_items(p)) == 1
    # 同じ id が複数箇所にあっても 1 件に畳む
    assert len(find_video_items({"a": {"itemList": [it]}, "b": {"x": [it]}})) == 1
    assert find_video_items({"nothing": 1}) == []


def test_extract_hashtags_from_both_sources():
    tags = extract_hashtags({"textExtra": [{"hashtagName": "購入品紹介"}],
                             "desc": "これ #便利グッズ と #購入品紹介"})
    assert "購入品紹介" in tags and "便利グッズ" in tags
    assert tags.count("購入品紹介") == 1        # 重複しない


def test_extract_product_anchor():
    a = extract_product_anchor(item())
    assert a["name"] == "充電式 毛玉取り器"
    assert extract_product_anchor(item(anchor=False)) is None
    # 種別が不明でも URL に product が入っていれば商品とみなす
    a2 = extract_product_anchor({"anchors": [{"type": 99, "keyword": "テスト",
                                              "url": "https://x/product/1"}]})
    assert a2 and a2["name"] == "テスト"


@pytest.mark.parametrize("desc,tags,anchor,lo,hi", [
    ("【購入品紹介】これ買ってよかった", ["購入品紹介"], False, 0.5, 1.0),
    ("今日のダンス", ["dance"], False, 0.0, 0.0),
    ("なんでもない", [], True, 1.0, 1.0),      # 商品リンク付きは確定
])
def test_product_intent_score(desc, tags, anchor, lo, hi):
    v = product_intent_score(desc, tags, anchor)
    assert lo <= v <= hi


# ---------------------------------------------------------------- 商品名の正規化

@pytest.mark.parametrize("a,b", [
    ("【充電式】毛玉取り器", "充電式 毛玉取り器"),
    ("充電式毛玉取り器", "充電式 毛玉取り器"),
    ("充電式 毛玉取り器！", "充電式 毛玉取り器"),
])
def test_product_name_variants_merge(a, b):
    assert product_key(a) == product_key(b)


@pytest.mark.parametrize("name,ok", [
    ("温感アイマスク", True), ("低温調理器 BONIQ", True),
    ("商品", False), ("リンクはこちら", False), ("詳細はこちら", False),
    ("プロフィールから", False), ("Link in bio", False), ("A5", False),
])
def test_junk_product_names_rejected(name, ok):
    """アンカーの文言が商品名でない場合、偽の商品に合流させない."""
    assert is_valid_product(name) is ok


# ---------------------------------------------------------------- 導出

def _vid(i, views, saves, creator, product, tags, now=None):
    m = {M.VIEWS: views, M.LIKES: views * 0.1, M.SAVES: saves,
         M.COMMENTS: views * 0.003, M.SHARES: views * 0.006}
    m[M.ENGAGEMENT_RATE] = (m[M.LIKES] + saves + m[M.COMMENTS] + m[M.SHARES]) / views
    m[M.SAVE_RATE] = saves / views
    m[M.VELOCITY] = views / 24
    return Snapshot(EntityType.VIDEO, f"v{i}", f"動画{i}", "tiktok_video", m, "JP",
                    extra={"creator": creator, "creator_name": creator,
                           "product": {"name": product} if product else None,
                           "hashtags": tags},
                    captured_at=now or time.time())


def test_rollup_merges_name_variants():
    vids = [_vid(1, 800_000, 30_000, "a", "【充電式】毛玉取り器", ["購入品紹介"]),
            _vid(2, 120_000, 3_000, "b", "充電式 毛玉取り器", ["時短"]),
            _vid(3, 45_000, 1_200, "c", "充電式毛玉取り器", ["便利グッズ"])]
    prods = rollup_products(vids, "JP")
    assert len(prods) == 1
    p = prods[0]
    assert p.metrics[M.VIDEO_COUNT] == 3
    assert p.metrics[M.CREATOR_COUNT] == 3
    assert p.metrics[M.TOTAL_VIEWS] == 965_000
    assert p.metrics[M.MEDIAN_VIEWS] == 120_000     # 合計ではなく中央値
    assert len(p.extra["top_videos"]) == 3


def test_median_not_inflated_by_one_viral_video():
    """まぐれの 1 本で中央値が跳ね上がらないこと (判断の根幹)."""
    vids = [_vid(0, 5_000_000, 100_000, "a", "毛玉取り器", ["t"])] + \
           [_vid(i, 3_000, 20, f"c{i}", "毛玉取り器", ["t"]) for i in range(1, 8)]
    p = rollup_products(vids, "JP")[0]
    assert p.metrics[M.TOTAL_VIEWS] > 5_000_000
    assert p.metrics[M.MEDIAN_VIEWS] == 3_000       # 中央値は現実を映す


def test_hit_rate_distinguishes_reliable_from_fluke():
    reliable = [_vid(i, 200_000, 6_000, f"c{i}", "温感アイマスク", ["t"]) for i in range(6)]
    fluke = [_vid(100, 3_000_000, 90_000, "z", "珪藻土バスマット", ["t"])] + \
            [_vid(100 + i, 1_000, 5, f"z{i}", "珪藻土バスマット", ["t"]) for i in range(1, 6)]
    out = {s.name: s for s in rollup_all(reliable + fluke, "JP")
           if s.entity_type is EntityType.PRODUCT}
    assert out["温感アイマスク"].metrics[M.HIT_RATE] > out["珪藻土バスマット"].metrics[M.HIT_RATE]


def test_videos_without_product_are_ignored():
    vids = [_vid(1, 1000, 10, "a", None, ["雑談"])]
    assert rollup_products(vids, "JP") == []


def test_rollup_hashtags_needs_multiple_videos():
    vids = [_vid(1, 1000, 10, "a", "毛玉取り器", ["共通", "ひとつだけ"]),
            _vid(2, 2000, 20, "b", "毛玉取り器", ["共通"])]
    tags = {s.name for s in rollup_hashtags(vids, "JP", min_videos=2)}
    assert "#共通" in tags
    assert "#ひとつだけ" not in tags     # 1 本しか使っていないタグはノイズ


def test_rollup_all_produces_three_kinds():
    vids = [_vid(i, 10_000 * (i + 1), 200 * (i + 1), f"c{i % 3}", "温感アイマスク",
                 ["購入品紹介", "便利グッズ"]) for i in range(6)]
    kinds = {s.entity_type for s in rollup_all(vids, "JP")}
    assert kinds == {EntityType.PRODUCT, EntityType.CREATOR, EntityType.HASHTAG}


def test_rollup_on_empty_input():
    assert rollup_all([], "JP") == []


# ---------------------------------------------------------------- 競合の山型

def test_competition_fit_is_inverted_u():
    """本数は少なすぎても多すぎても不利。中庸が最も高い."""
    one, _ = competition_fit(1)
    sweet, _ = competition_fit(10)
    many, _ = competition_fit(400)
    assert sweet > one and sweet > many
    assert competition_fit(None)[0] == 0.5      # 不明なら中立
