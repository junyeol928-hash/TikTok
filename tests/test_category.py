"""食べ物かどうかの判定.

物 (グッズ・家電・雑貨) を紹介したい人向けなので、
食べ物は分析対象から外す。ただし **食器・炊飯器・弁当箱** のような
「食にまつわる物」まで落としてはいけない。ここが崩れると
撮る候補がごっそり消えるので、両方向をテストで固める。
"""

import pytest

from ttradar.analysis.category import is_food, looks_like_goods


@pytest.mark.parametrize("text", [
    "コンビニスイーツ食べ比べしてみた",
    "話題のラーメン屋に行ってきた #グルメ",
    "簡単レシピ 作り置きおかず",
    "タピオカ飲み比べ",
    "プロテインの飲み比べ",              # 口に入れるものは物ではない
    "宅飲みにおすすめのビール",
    "mukbang challenge",
    "seafood platter",                   # food が語として出る
    "カフェ巡りの記録 #カフェ",
    "業務スーパーの購入品紹介",
])
def test_food_is_detected(text):
    assert is_food(text) is True


@pytest.mark.parametrize("text", [
    "ダイソーの食器がかわいすぎた",       # 食器は物
    "食洗機対応のタッパー買った",
    "弁当箱の購入品紹介 #ランチボックス",
    "水筒とタンブラーを比較してみた",
    "炊飯器を買い替えた正直レビュー",
    "圧力鍋のレビュー",
    "充電式 毛玉取り器のレビュー",
    "神アイテム収納ボックス",
    "スキンケア購入品まとめ",
    "折りたたみ水切りラックが優秀",
    "",
])
def test_goods_are_kept(text):
    assert is_food(text) is False


def test_goods_override_beats_food_words():
    """食の語を含んでいても、物と分かる語があれば物として扱う."""
    assert looks_like_goods("お菓子作りの型 #キッチングッズ") is True
    assert is_food("お菓子作りの型 #キッチングッズ") is False


def test_multiple_fields_are_joined():
    """キャプション・タグ・商品名をまとめて見る."""
    assert is_food("これ買ってよかった", "#スイーツ #購入品紹介") is True
    assert is_food("これ買ってよかった", "#便利グッズ", "毛玉取り器") is False


@pytest.mark.parametrize("text", [
    "made in japan",       # food/ad の部分一致で落ちてはいけない
    "spring collection",
    "cafeteria style shelf",   # cafe の部分一致
])
def test_ascii_markers_need_word_boundaries(text):
    assert is_food(text) is False
