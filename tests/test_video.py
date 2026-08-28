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
                                             product_intent_detail,
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


@pytest.mark.parametrize("desc,tags", [
    ("I made this at home", ["fyp"]),        # "ad" が made に一致してはいけない
    ("Spring vibes 2026", ["fyp"]),          # "pr" が spring に一致してはいけない
    ("my dad is so ready", []),
    ("ADHD あるある", []),
    ("grandma reaction", ["cat"]),
])
def test_ascii_markers_do_not_match_inside_words(desc, tags):
    """英字の目印は語として一致させる.

    部分一致だと商品紹介ではない動画が丸ごと分析対象に混ざる。
    「商品紹介動画だけを見る」という前提そのものが崩れるので回帰テストを置く。
    """
    assert product_intent_score(desc, tags, False) == 0.0


@pytest.mark.parametrize("desc,tags", [
    ("honest review of this", ["ad"]),
    ("summer haul unboxing", ["haul"]),
    ("提供です #PR", ["pr"]),
])
def test_ascii_markers_match_as_words(desc, tags):
    assert product_intent_score(desc, tags, False) > 0.0


@pytest.mark.parametrize("label,item", [
    ("従来の anchors",
     {"anchors": [{"type": 46, "keyword": "充電式 毛玉取り器",
                   "schema": "https://shop.tiktok.com/view/product/17293"}]}),
    ("入れ子",
     {"anchorInfo": {"anchors": [{"type": 47, "keyword": "収納ボックス",
                                  "schema": "aweme://shop/product?product_id=99"}]}}),
    ("商品 ID だけ",
     {"extra": {"deep": {"productId": "123", "title": "ミニ加湿器"}}}),
    ("URL だけ",
     {"foo": {"title": "ハンディファン",
              "url": "https://www.tiktok.com/view/product/555"}}),
])
def test_anchor_is_found_by_shape(label, item):
    """アンカーの置き場所が変わっても商品を取り出せること.

    キー名を決め打ちすると、TikTok が構造を変えた瞬間に
    商品が 1 件も取れない状態へ静かに陥る。
    """
    a = extract_product_anchor(item)
    assert a and a["name"], label


@pytest.mark.parametrize("item", [
    # 種別が一致するだけの無関係な dict を拾ってはいけない
    {"music": {"type": 2, "title": "流行りの曲",
               "url": "https://sf.tiktok.com/music/1"}},
    {"author": {"nickname": "someone"}, "desc": "ふつうの動画"},
])
def test_anchor_does_not_false_positive(item):
    assert extract_product_anchor(item) is None


def test_product_intent_detail_returns_evidence():
    """判定の根拠を返す. UI で「なぜ商品紹介と見なしたか」を出すのに使う."""
    score, words = product_intent_detail("【購入品紹介】買ってよかった", ["レビュー"], False)
    assert score > 0.5
    assert "購入" in words and "レビュー" in words
    # 商品リンク付きは確定だが、語の根拠も一緒に返す
    assert product_intent_detail("なんでもない", [], True) == (1.0, [])


@pytest.mark.parametrize("url", [
    # 実機で漏れていたもの。決め打ちの一覧だと静かに 0 件になっていた
    "https://www.tiktok.com/api/prefetch/explore/item_list/?x=1",
    "https://www.tiktok.com/api/search/item/full/?q=a",
    "https://www.tiktok.com/api/search/general/full/?q=a",
    "https://www.tiktok.com/api/challenge/item_list/?id=1",
    "https://www.tiktok.com/api/post/item_list/?id=1",
    "https://www.tiktok.com/api/recommend/item_list/",
])
def test_item_list_urls_are_intercepted(url):
    from ttradar.collectors.tiktok_video import is_item_list_url
    assert is_item_list_url(url)


@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/api/global-footer/graphql",
    "https://www.tiktok.com/api/share/settings/",
    "https://www.tiktok.com/api/v1/web-cookie-privacy/config",
    "https://www.tiktok.com/node-webapp/api/importmap",
])
def test_unrelated_api_is_not_intercepted(url):
    from ttradar.collectors.tiktok_video import is_item_list_url
    assert not is_item_list_url(url)


def test_early_stop_counts_usable_videos():
    """打ち切りの判定は「使える本数」で行うこと.

    ハッシュタグページは人気順なので古い動画が大量に混ざる。
    生の件数で打ち切ると「245 件集めたが 130 件は古くて使えない」
    という取りこぼしが起きる。
    """
    from ttradar.collectors.tiktok_video import TikTokVideoCollector
    from ttradar.config import Config

    c = TikTokVideoCollector(Config())
    c.config.raw["max_video_age_days"] = 30
    now = time.time()
    fresh = [{"id": str(i), "createTime": int(now - 5 * 86400)} for i in range(50)]
    stale = [{"id": str(1000 + i), "createTime": int(now - 200 * 86400)}
             for i in range(130)]
    dups = [{"id": "1", "createTime": int(now - 5 * 86400)} for _ in range(20)]

    assert c._usable_count(fresh + stale + dups) == 50, "古い動画や重複を数えている"
    # 期間の制限が無ければ全部が対象 (重複は除く)
    c.config.raw["max_video_age_days"] = 0
    assert c._usable_count(fresh + stale + dups) == 180


def test_collector_dedupes_across_pages():
    """同じ動画が複数のタグに出てきても 1 本として数えること."""
    from ttradar.collectors.base import dedupe

    def v(i):
        return Snapshot(entity_type=EntityType.VIDEO, native_id=str(i),
                        name="x", source="tiktok_video",
                        metrics={M.VIEWS: 1.0}, region="JP")

    assert len(dedupe([v(1), v(2), v(1), v(3), v(2)])) == 3


def test_hashtag_pages_come_before_search():
    """検索は未ログインだと弾かれるので、タグページを先に見る."""
    from ttradar.collectors.tiktok_video import TikTokVideoCollector
    from ttradar.config import Config

    cfg = Config()
    cfg.raw["video_queries"] = ["購入品紹介", "便利グッズ"]
    t = TikTokVideoCollector(cfg).build_targets()
    urls = [u for _, u in t]
    first_search = next(i for i, u in enumerate(urls) if "/search/" in u)
    last_tag = max(i for i, u in enumerate(urls) if "/tag/" in u)
    assert last_tag < first_search, "検索がタグページより先に来ている"
    assert len(urls) == len(set(urls)), "同じ URL を二度見に行っている"


@pytest.mark.parametrize("hint,expect_lines", [
    ("購入品紹介 の動画 12件 おすすめ", False),
    ("不明なエラーが発生しました もう一度お試しください ログイン", True),
    ("認証を完了してください captcha", True),
])
def test_diagnose_block(hint, expect_lines):
    """0 件のとき、次に何をすればいいかを出せること."""
    from ttradar.collectors.tiktok_video import diagnose_block
    lines = diagnose_block(hint)
    assert bool(lines) is expect_lines
    if lines:
        assert any("ログイン" in x or "認証" in x for x in lines)


def test_products_are_derived_without_shop_links():
    """商品リンクが無い動画からも商品が作られること.

    日本では Shop リンク付きの動画が少ないので、リンク必須にすると
    伸びている紹介動画の大半が「商品不明」として捨てられてしまう。
    """
    from ttradar.analysis.rollup import rollup_products

    def vid(i, cap):
        return Snapshot(
            entity_type=EntityType.VIDEO, native_id=f"n{i}", name=cap,
            source="tiktok_video",
            metrics={M.VIEWS: 100_000.0 + i, M.SAVES: 3000.0},
            region="JP",
            extra={"creator": f"c{i}", "hashtags": ["購入品紹介"],
                   "product": None,
                   "product_candidates": [
                       {"name": "ダイソーの収納ケース",
                        "confidence": 0.9, "source": "caption"}]},
        )

    prods = rollup_products([vid(1, "a"), vid(2, "b")], "JP")
    assert len(prods) == 1
    p = prods[0]
    assert p.name == "ダイソーの収納ケース"
    assert p.metrics[M.VIDEO_COUNT] == 2
    # 推定であることと、根拠の動画が残っていること
    assert p.extra["name_source"] == "caption"
    assert 0 < p.extra["name_confidence"] < 1.0
    assert len(p.extra["top_videos"]) == 2


def test_low_confidence_names_are_dropped():
    """自信の無い推定は商品にしない (一覧がゴミで埋まるのを防ぐ)."""
    from ttradar.analysis.rollup import rollup_products

    v = Snapshot(
        entity_type=EntityType.VIDEO, native_id="x", name="なにか",
        source="tiktok_video", metrics={M.VIEWS: 50_000.0}, region="JP",
        extra={"creator": "c", "hashtags": [],
               "product_candidates": [
                   {"name": "よくわからないもの", "confidence": 0.35,
                    "source": "caption"}]},
    )
    assert rollup_products([v], "JP") == []


def test_old_snapshots_without_candidates_still_work():
    """product_candidates を持たない過去のデータでも商品が作れること."""
    from ttradar.analysis.rollup import rollup_products

    v = Snapshot(
        entity_type=EntityType.VIDEO, native_id="y", name="レビュー",
        source="tiktok_video", metrics={M.VIEWS: 50_000.0}, region="JP",
        extra={"creator": "c", "hashtags": [],
               "product": {"name": "充電式 毛玉取り器", "url": "https://x/p/1"}},
    )
    prods = rollup_products([v], "JP")
    assert prods and prods[0].name == "充電式 毛玉取り器"
    assert prods[0].extra["name_source"] == "anchor"


def test_parse_item_records_product_candidates():
    """収集時にキャプションからの商品候補が保存されること."""
    it = item(anchor=False)
    it["desc"] = "ダイソーの新作収納ケースが優秀すぎた #購入品紹介"
    snap = parse_item(it, "JP", "tiktok_video")
    cands = snap.extra["product_candidates"]
    assert cands and cands[0]["name"] == "ダイソーの新作収納ケース"
    assert cands[0]["source"] == "caption"


def test_parse_item_records_intent_evidence():
    snap = parse_item(item(), "JP", "tiktok_video")
    assert snap.extra["product"]["name"] == "充電式 毛玉取り器"
    assert isinstance(snap.extra["intent_words"], list)


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
