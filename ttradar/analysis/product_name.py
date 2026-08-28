"""紹介動画のキャプションから「何を紹介しているか」を取り出す.

なぜ必要か
----------
TikTok Shop の商品リンクが付いている動画は日本ではまだ少ない。
リンク付きだけを商品として扱うと、伸びている紹介動画の大半が
「商品が分からないもの」として捨てられてしまう。

知りたいのは購入リンクではなく **どういう商品が紹介されているか** なので、
キャプションとハッシュタグから商品名らしい語を取り出す。
正確な型番までは要らない。「ダイソーの収納ケース」「充電式 毛玉取り器」
くらいの粒度が出れば、そこから先は自分で調べられる。

やり方
------
形態素解析器は使わない (Windows での導入手順を増やしたくないため)。
代わりに日本語の書き方の性質を使う:

- 商品名は **漢字・カタカナ・英数字** の連なりでできている
- ひらがなは助詞や活用語尾なので、そこが語の切れ目になる
- 「の」だけは商品名の内部に入る (ダイソー**の**収納ケース)

この性質で候補を切り出し、次の 3 つで確からしさを付ける:

1. カテゴリ語を含むか (ケース・ラック・クリーナー…) — 物である決定的な証拠
2. ブランド名を含むか (ダイソー・無印良品・SHEIN…)
3. レビュー用語そのもの (レビュー・購入品紹介・神アイテム) でないか

確からしさは UI に出すので、外れていても利用者が気付ける。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 商品のカテゴリを表す語。これを含む候補は「物」とみなしてよい。
#: 「アイテム」「グッズ」「商品」のような総称は入れない (何も特定できないため)。
CATEGORY_WORDS: tuple[str, ...] = (
    # 収納・生活
    "ケース", "ボックス", "ラック", "スタンド", "ホルダー", "ハンガー", "フック",
    "収納", "棚", "ワゴン", "カゴ", "かご", "トレー", "ポーチ", "バスケット",
    "仕切り", "ストッカー", "ハンギング", "突っ張り", "つっぱり",
    # 掃除・洗濯
    "クリーナー", "ワイパー", "ブラシ", "モップ", "洗剤", "スポンジ", "手袋",
    "毛玉取り", "コロコロ", "ハンディ", "掃除機", "洗濯", "物干し", "ピンチ",
    # 家電・ガジェット
    "ドライヤー", "アイロン", "スチーマー", "加湿器", "除湿", "空気清浄",
    "扇風機", "サーキュレーター", "ヒーター", "ライト", "ランプ", "照明",
    "充電器", "ケーブル", "バッテリー", "イヤホン", "ヘッドホン", "スピーカー",
    "プロジェクター", "カメラ", "時計", "体重計", "シェーバー", "カッター",
    "トリマー", "ミシン", "電動", "コードレス", "ワイヤレス", "モニター",
    "キーボード", "マウス", "タブレット", "スマホケース", "リングライト",
    # 寝具・インテリア
    "枕", "まくら", "布団", "毛布", "パッド", "シーツ", "カーテン", "ラグ",
    "マット", "クッション", "ソファ", "チェア", "デスク", "テーブル",
    "ミラー", "鏡", "アロマ", "ディフューザー", "ブランケット", "スリッパ",
    "ルームシューズ", "タオル", "ボトル", "ポット",
    # ファッション
    "シャツ", "パーカー", "ニット", "ワンピース", "スカート", "パンツ",
    "デニム", "コート", "ジャケット", "バッグ", "リュック", "財布", "靴",
    "スニーカー", "サンダル", "ブーツ", "帽子", "キャップ", "ベルト",
    "ソックス", "靴下", "ピアス", "ネックレス", "腕時計", "メガネ", "サングラス",
    "インナー", "ブラ", "ルームウェア", "パジャマ",
    # 美容
    "コスメ", "リップ", "グロス", "チーク", "ファンデ", "コンシーラー",
    "アイシャドウ", "マスカラ", "アイライナー", "アイブロウ", "カラコン",
    "日焼け止め", "化粧水", "乳液", "美容液", "クリーム", "パック",
    "シャンプー", "トリートメント", "ヘアオイル", "ボディクリーム", "香水",
    "ネイル", "ヘアアイロン", "コテ", "洗顔", "クレンジング", "下地",
    "マスク", "シートマスク", "リップクリーム", "ハンドクリーム",
    # キッチン (物として扱う。中身の食品は category.py 側で除外)
    "フライパン", "包丁", "まな板", "水切り", "タンブラー", "水筒", "マグ",
    "保存容器", "ラップ", "おろし器", "ピーラー", "弁当箱", "食器", "ケトル",
    # 文具・その他
    "ペン", "ノート", "手帳", "シール", "テープ", "はさみ", "傘", "レイン",
    "キーケース", "カバー", "シート", "リング", "クリップ", "マグネット",
)

#: ブランド・店名。含まれていれば商品名らしさが上がる。
BRAND_WORDS: tuple[str, ...] = (
    "ダイソー", "daiso", "セリア", "キャンドゥ", "3coins", "スリーコインズ",
    "スリコ", "無印良品", "無印", "ニトリ", "ikea", "イケア", "カインズ",
    "ワークマン", "ユニクロ", "uniqlo", "gu", "しまむら", "shein", "temu",
    "amazon", "アマゾン", "楽天", "ドンキ", "ドン・キホーテ", "ロフト", "loft",
    "ハンズ", "プラザ", "マツキヨ", "ウエルシア", "サンリオ", "ちいかわ",
    "キャンメイク", "セザンヌ", "ケイト", "ちふれ", "資生堂", "花王",
    "パナソニック", "シャープ", "アイリスオーヤマ", "象印", "タイガー",
    "バルミューダ", "ダイソン", "dyson", "anker", "シャークニンジャ",
    "ブラウン", "フィリップス", "サロニア", "salonia", "リファ", "refa",
    "ヤーマン", "コジット", "山崎実業", "towerシリーズ", "tower",
)

#: 商品名ではなく「動画の型」を指す語。単体では商品にならない。
_REVIEW_WORDS: frozenset[str] = frozenset({
    "レビュー", "正直レビュー", "本音レビュー", "購入品", "購入品紹介",
    "商品紹介", "紹介", "おすすめ", "神アイテム", "便利グッズ", "買ってよかった",
    "開封", "開封動画", "本音", "正直", "比較", "使ってみた", "リピート",
    "コスパ", "最強", "優秀", "商品", "アイテム", "グッズ", "モノ", "もの",
    "今年", "今月", "今週", "最近", "話題", "人気", "新作", "新商品",
    "セール", "クーポン", "割引", "限定", "プチプラ", "高見え", "映え",
    "тiktok", "tiktok", "fyp", "pr", "ad", "提供", "宣伝",
    "円", "円以下", "円以内", "円台",
})

#: 商品ではなくジャンル・売り場を指す語。単体では商品にならない。
#: これを商品として並べると「コスメ 74本」のような無意味な行が上位に来る。
GENRE_WORDS: frozenset[str] = frozenset({
    "コスメ", "美容", "美容家電", "スキンケア", "ヘアケア", "ボディケア",
    "家電", "時短家電", "調理家電", "ガジェット", "インテリア", "収納",
    "暮らし", "暮らしを整える", "掃除", "掃除グッズ", "便利グッズ",
    "キッチン", "キッチングッズ", "ファッション", "コーデ", "プチプラ",
    "推し活", "ヲタ活", "ハック", "ライフハック", "時短", "節約", "収納術",
    "一人暮らし", "同棲", "新生活", "ホーム", "生活", "日用品", "雑貨",
})

#: 括弧の中身。【本音】のようなラベルも入るので score で落とす。
_BRACKET_RE = re.compile(r"[【「『\[]([^】」』\]]{2,30})[】」』\]]")

#: 数字だけ・記号だけの候補を落とす
_ONLY_SYMBOLS_RE = re.compile(r"^[0-9０-９ー－\-\+＋\s]*$")


@dataclass(frozen=True)
class Candidate:
    """商品名の候補."""

    name: str
    #: 0-1。カテゴリ語やブランドを含むほど高い
    confidence: float
    #: どこから取ったか: anchor / caption / hashtag
    source: str

    def label_ja(self) -> str:
        return {"anchor": "リンク確定", "caption": "キャプションから",
                "hashtag": "タグから"}.get(self.source, self.source)


def _is_review_word(s: str) -> bool:
    return s.lower().strip() in _REVIEW_WORDS


def has_category_word(s: str) -> bool:
    return any(w in s for w in CATEGORY_WORDS)


def has_brand_word(s: str) -> bool:
    low = s.lower()
    return any(w in low for w in BRAND_WORDS)


def _is_genre_word(s: str) -> bool:
    return s.lower().strip().replace(" ", "") in GENRE_WORDS


def _is_all_generic(name: str) -> bool:
    """ジャンル語とレビュー用語を取り除くと何も残らないか.

    「コスメ購入品」のような、単体では弾けないが中身の無い組み合わせを落とす。
    """
    rest = name.lower().replace(" ", "").replace("　", "")
    for w in sorted(GENRE_WORDS | _REVIEW_WORDS, key=len, reverse=True):
        rest = rest.replace(w, "")
    return len(rest.strip("のとやも、・")) < 2


def _score(name: str) -> float:
    """商品名らしさ. カテゴリ語が最も強い証拠."""
    if (_is_review_word(name) or _is_genre_word(name)
            or len(name) < 3 or _is_all_generic(name)):
        return 0.0
    cat, brand = has_category_word(name), has_brand_word(name)
    if cat and brand:
        return 0.9        # 「ダイソーの収納ケース」
    if cat:
        return 0.75       # 「折りたたみ水切りラック」
    if brand:
        return 0.55       # 「無印良品の新作」— 何かは分からないが商品の話
    # カテゴリもブランドも無い。長めの固有名詞なら弱い候補として残す
    return 0.35 if len(name) >= 5 else 0.0


def _clean(s: str) -> str:
    """ハッシュタグ・URL・絵文字を落とす. 絵文字は語の区切りとして働く."""
    s = re.sub(r"[#＃]\S+", " ", str(s))
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\s　ぁ-ヿ一-鿿【】「」『』\[\]:：、。，．,.!！?？ー－\-\+＋]",
               " ", s)
    return s


#: 文の区切り。ここで切ってから商品名を探す
_SPLIT_RE = re.compile(r"[、。，．,.!！?？…‥\n\r/／|｜・･]+|\s{2,}")

#: ひらがな以外で始まり、内部に短いひらがなを含んでよい語のかたまり。
#: 商品名には「折**り**たたみ」「毛玉**取り**器」のようにひらがなが入るので、
#: ひらがなを一切許さないと名前が途中で切れてしまう。
#: ただし長いひらがなの連なりは文なので、4 文字までに制限する。
_NAME_RUN = re.compile(
    r"[一-鿿々ァ-ヾA-Za-z0-9]"                 # 先頭はひらがな以外
    r"(?:[一-鿿々ァ-ヾA-Za-z0-9ー－\-\+＋ ]|[ぁ-ゖ]{1,4}(?=[一-鿿々ァ-ヾA-Za-z0-9]))*"
)

#: 商品名の後ろに付く言い回し。末尾から繰り返し剥がす。
_TAIL_RE = re.compile(
    r"(?:使ってみた|使ってる|使って|使った|買ってよかった|買ってみた|買った|買って|"
    r"試してみた|試した|届いた|比べて|比較|レビュー|紹介|開封|購入|"
    r"でした|です|だった|すぎた|すぎる|過ぎた|最高|優秀|神|欲しい|良い|いい|"
    r"って|けど|から|まで|より|など|とか|なら|ので|のに|"
    r"正直|本音|感想|結果|話|件|編|選|"
    r"\d+\s*(?:週間|日間|日|ヶ月|か月|カ月|年|回|個|本|点)|"
    r"[がはをにでともやかねよのしただ]"
    r")+$")

#: 商品名の前に付く言い回し。先頭から剥がす。
_HEAD_RE = re.compile(
    r"^(?:これ|それ|この|その|今回|今年|今月|今週|最近|話題|本当に|まじで|"
    r"全部|ずっと|やっと|ついに|絶対|マジで|正直|本音|新作|新商品|人気|"
    r"買ってよかった|使ってる|愛用中|愛用|おすすめ|オススメ|"
    r"正直レビュー|本音レビュー|レビュー|紹介|開封|購入品紹介|購入品|"
    r"[:：\-ー－\s]+|"
    r"[ぁ-ゖ]{1,3}(?=[一-鿿々ァ-ヾA-Za-z0-9]))")


def _strip_grammar(s: str) -> str:
    """前後の言い回しを剥がして商品名だけにする.

    末尾の長音記号は削らない。「バッテリー」「クリーナー」の
    最後の一文字が消えてしまうため。
    """
    prev = None
    while prev != s and s:
        prev = s
        s = _TAIL_RE.sub("", s).rstrip(" 　-－").lstrip(" 　ー－-")
        s = _HEAD_RE.sub("", s).rstrip(" 　-－").lstrip(" 　ー－-")
    return s


#: 商品名としてありうる長さの上限。これを超えるものは文になっている
MAX_NAME_LEN = 20


def _trim_len(name: str) -> str:
    """長すぎる候補を右端 (カテゴリ語側) を残して詰める."""
    if len(name) <= MAX_NAME_LEN:
        return name
    return _strip_grammar(name[-MAX_NAME_LEN:])


#: カテゴリ語の直後に続きうる接尾辞。「毛玉取り」で切ると「器」が落ちる。
_SUFFIXES = ("器", "機", "用", "剤", "棒", "台", "皿", "袋", "箱", "具", "品",
             "型", "式", "付", "セット", "ケース", "カバー")


#: カテゴリ語の後ろに続く容量・枚数の表記。ここまで含めて 1 つの商品名。
#: 「シートマスク 30枚」を「シートマスク」で切ると別商品と区別できなくなる。
_SPEC_RE = re.compile(
    r"^\s?\d+(?:\.\d+)?\s?(?:枚入り|枚入|枚|個入り|個入|個|本入|本|"
    r"ml|ML|mL|L|g|G|kg|cm|mm|m|色|袋|巻|セット|点|パック|way|WAY)")


def _extend_suffix(seg: str, end: int) -> int:
    """カテゴリ語の終わりから、接尾辞と容量表記のぶんだけ右に伸ばす."""
    for suf in _SUFFIXES:
        if seg.startswith(suf, end):
            end += len(suf)
            break
    m = _SPEC_RE.match(seg[end:])
    if m:
        end += m.end()
    return end


def _cut_at_category(seg: str) -> list[str]:
    """カテゴリ語で終わるところまでを商品名として切り出す.

    「ダイソーの新作収納ケースが優秀すぎた」→「ダイソーの新作収納ケース」

    商品名は必ずカテゴリ語 (ケース・ラック・クリーナー…) で終わるという
    日本語の性質を使う。末尾の言い回しを 1 つずつ剥がすより確実で、
    「ヘアアイロン使ってみた」のような取りこぼしも起きない。
    """
    # 終端が同じ位置になる切り方が複数あるときは、より長いカテゴリ語を使う
    # (「シートマスク」を「シート」で切らないため)
    ends: dict[int, str] = {}
    for w in CATEGORY_WORDS:
        i = seg.rfind(w)
        if i < 0:
            continue
        e = _extend_suffix(seg, i + len(w))
        if len(w) > len(ends.get(e, "")):
            ends[e] = w

    out: list[str] = []
    for end, w in ends.items():
        # 商品名は長くても 24 文字程度。左側は窓で切ってから言い回しを剥がす
        left = max(0, end - 24)
        name = _trim_len(_strip_grammar(seg[left:end]))
        if len(name) >= 3:
            out.append(name)
    return out


def extract_from_caption(desc: str) -> list[Candidate]:
    """キャプションから商品名の候補を返す (確からしさの高い順)."""
    if not desc:
        return []
    # 【本音】【検証】のような型のラベルは本文から外す。
    # 残すと「【本音】充電式 毛玉取り器」が商品名になってしまう。
    body = _BRACKET_RE.sub(
        lambda m: " " if _score(m.group(1).strip()) == 0 else m.group(0), str(desc))
    text = _clean(body)
    found: list[tuple[str, float]] = []

    # 1) 括弧の中身。商品名が入っていることがある (【ダイソー新商品】など)
    for m in _BRACKET_RE.finditer(str(desc)):
        name = _strip_grammar(m.group(1).strip())
        if len(name) >= 3 and _score(name) > 0:
            found.append((name, min(1.0, _score(name) + 0.05)))

    # 2) 文ごとに、カテゴリ語で終わる商品名を切り出す。
    #    カテゴリ語が見つかった文では、それ以外の切り出し方は使わない。
    #    文まるごとを拾う塊はカテゴリ語で切ったものより必ず長くなるので、
    #    混ぜると「サロニアのヘアアイロン使ってみた正直」のような
    #    文の切れ端が勝ってしまう。
    for seg in _SPLIT_RE.split(text):
        seg = seg.strip()
        if len(seg) < 3:
            continue
        cut = [(n, _score(n)) for n in _cut_at_category(seg)]
        cut = [(n, sc) for n, sc in cut if sc > 0]
        if cut:
            found.extend(cut)
            continue
        # 3) カテゴリ語が無い文でも、固有名詞らしい塊は弱い候補にする
        #    (未知のカテゴリの商品を取りこぼさないため)
        for m in _NAME_RUN.finditer(seg):
            name = _trim_len(_strip_grammar(m.group(0).strip()))
            if len(name) < 4 or _ONLY_SYMBOLS_RE.match(name):
                continue
            sc = _score(name)
            if sc > 0:
                found.append((name, sc))

    best: dict[str, tuple[str, float]] = {}
    for name, sc in found:
        k = name.lower().replace(" ", "")
        prev = best.get(k)
        if prev is None or sc > prev[1] or (sc == prev[1] and len(name) > len(prev[0])):
            best[k] = (name, sc)

    # 短い候補が長い候補に完全に含まれるなら、長い方 (情報が多い) を残す
    items = sorted(best.values(), key=lambda x: -len(x[0]))
    kept: list[tuple[str, float]] = []
    for name, sc in items:
        k = name.lower().replace(" ", "")
        if any(k in other.lower().replace(" ", "") for other, _ in kept):
            continue
        kept.append((name, sc))

    ranked = sorted(kept, key=lambda x: (-x[1], -len(x[0])))
    return [Candidate(n, s, "caption") for n, s in ranked]


def extract_from_hashtags(tags: list[str]) -> list[Candidate]:
    """ハッシュタグから商品名の候補を返す.

    ``#毛玉取り器`` のようなタグは商品そのものを指す。
    ``#購入品紹介`` のような型のタグは score で落ちる。
    """
    out: list[Candidate] = []
    for t in tags or []:
        name = str(t).lstrip("#").strip()
        if len(name) < 3:
            continue
        sc = _score(name)
        if sc > 0:
            # タグは短縮されがちなのでキャプションより低く見る
            out.append(Candidate(name, max(0.3, sc - 0.15), "hashtag"))
    return sorted(out, key=lambda c: -c.confidence)


def extract_products(desc: str, hashtags: list[str] | None = None,
                     anchor: dict | None = None,
                     limit: int = 3,
                     min_confidence: float = 0.5) -> list[Candidate]:
    """1 本の動画が紹介している商品の候補を返す.

    商品リンクがあればそれが最優先 (確定)。
    無ければキャプション → ハッシュタグの順に候補を作る。

    ``limit`` を 1 より大きくするのは、1 本の動画で複数の商品を
    紹介する「購入品まとめ」形式が多いため。
    """
    out: list[Candidate] = []
    if isinstance(anchor, dict) and anchor.get("name"):
        out.append(Candidate(str(anchor["name"]).strip(), 1.0, "anchor"))

    seen = {c.name.lower().replace(" ", "") for c in out}
    for c in extract_from_caption(desc) + extract_from_hashtags(hashtags or []):
        if c.confidence < min_confidence:
            continue
        k = c.name.lower().replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
        if len(out) >= limit:
            break
    return out
