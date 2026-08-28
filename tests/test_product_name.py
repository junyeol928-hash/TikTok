"""キャプションから商品名を取り出す処理のテスト.

日本では TikTok Shop の商品リンク付き動画が少ないので、
ここが機能しないと「どういう商品が伸びているか」が出せない。
本システムの中心的な機能なので、取れること・取りすぎないことの両方を固める。
"""

import pytest

from ttradar.analysis.product_name import (Candidate, extract_from_hashtags,
                                           extract_products, has_brand_word,
                                           has_category_word)


@pytest.mark.parametrize("caption,expect", [
    # カテゴリ語で終わるところまでを商品名として切り出せる
    ("ダイソーの新作収納ケースが優秀すぎた", "ダイソーの新作収納ケース"),
    ("サロニアのヘアアイロン使ってみた正直レビュー", "サロニアのヘアアイロン"),
    ("3COINSの新作ミニ加湿器買ってみた", "3COINSの新作ミニ加湿器"),
    ("シャークニンジャのハンディクリーナー買った", "シャークニンジャのハンディクリーナー"),
    # ひらがなを含む商品名が途中で切れない
    ("正直レビュー: 折りたたみ水切りラック 使って1週間", "折りたたみ水切りラック"),
    ("これ買ってよかった…充電式 毛玉取り器", "充電式 毛玉取り器"),
    # 長音記号を落とさない
    ("Ankerのモバイルバッテリーがコスパ最強だった", "Ankerのモバイルバッテリー"),
    # 【本音】のような型のラベルは商品名に混ぜない
    ("【本音】充電式 毛玉取り器は買いなのか", "充電式 毛玉取り器"),
])
def test_extracts_expected_name(caption, expect):
    got = extract_products(caption, [])
    assert got, f"何も取れなかった: {caption}"
    assert got[0].name == expect


@pytest.mark.parametrize("caption,tags", [
    ("猫がかわいすぎる件について", ["cat", "fyp"]),
    ("今週の購入品紹介", ["fyp", "おすすめにのりたい"]),
    ("1000円以下で買える便利グッズが優秀すぎた", ["便利グッズ", "おすすめ"]),
    ("神アイテム見つけた", ["神アイテム"]),
])
def test_does_not_invent_products(caption, tags):
    """具体的な商品名が無いキャプションから商品をでっち上げないこと.

    「便利グッズ」「購入品紹介」は動画の型であって商品ではない。
    ここを取ってしまうと、商品一覧がレビュー用語で埋まって使えなくなる。
    """
    assert extract_products(caption, tags) == []


def test_anchor_wins_and_is_marked():
    """商品リンクがあればそれが最優先で、出どころが分かること."""
    got = extract_products("これ良かった", ["購入品紹介"],
                           anchor={"name": "充電式 毛玉取り器"})
    assert got[0] == Candidate("充電式 毛玉取り器", 1.0, "anchor")
    assert got[0].label_ja() == "リンク確定"


def test_caption_candidates_are_marked_as_estimates():
    got = extract_products("ダイソーの収納ケースが便利", [])
    assert got[0].source == "caption"
    assert got[0].label_ja() == "キャプションから"
    assert 0 < got[0].confidence < 1.0, "推定なのに確定扱いになっている"


def test_category_and_brand_raise_confidence():
    """カテゴリ語とブランドの両方を含むほど確からしい."""
    both = extract_products("ニトリの収納ボックス", [])[0].confidence
    cat = extract_products("折りたたみ収納ボックス", [])[0].confidence
    assert both > cat


def test_hashtags_are_a_weaker_source():
    tags = extract_from_hashtags(["毛玉取り", "購入品紹介", "fyp"])
    names = [c.name for c in tags]
    assert "毛玉取り" in names
    assert "購入品紹介" not in names and "fyp" not in names
    assert all(c.source == "hashtag" for c in tags)


def test_multiple_products_per_video():
    """まとめ形式の動画から複数の商品を取れること (購入品まとめが多いため)."""
    got = extract_products(
        "ダイソーの収納ケースと無印良品のポリプロピレンボックスを比較", [])
    assert len(got) >= 2


def test_helpers():
    assert has_category_word("収納ケース") and not has_category_word("神")
    assert has_brand_word("ニトリの何か") and not has_brand_word("その他")
