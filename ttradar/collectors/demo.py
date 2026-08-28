"""オフライン検証用のデモ collector.

TikTok に到達できない環境 (CI / 制限付きネットワーク / 初回セットアップ確認) でも
パイプライン全体を動かせるようにするためのもの。

**本番と同じ経路を通す**のが方針。
つまり生成するのは VIDEO スナップショットだけで、
商品・クリエイター・ハッシュタグは本番同様 rollup に導出させる。
これにより「動画 → 商品」の変換自体もオフラインで検証できる。

単なるランダムではなく **意図的に色々な性質の商品を作る**:
再現性の高いもの / まぐれ 1 本だけのもの / 飽和したもの / 立ち上がり中のもの。
これによりスコアリングが意図通り効いているか確認できる。
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from typing import Any

from ..models import EntityType, M, Snapshot
from .base import Collector, register

#: (商品名, カテゴリ, 動画本数, 基準再生数, 保存率, エンゲージ率, 再現性, 形状)
#: 再現性 = その商品の動画がどれくらい揃って伸びるか (0-1)
#: 形状: explosive=爆発 / steady=順調 / decel=鈍化 / decline=下降 / flat=横ばい
PRODUCT_SEEDS: list[tuple[str, str, int, float, float, float, float, str]] = [
    ("充電式 毛玉取り器",       "生活",     8,  120_000, 0.038, 0.115, 0.75, "surge"),
    ("温感アイマスク 20枚入",   "美容",    14,   88_000, 0.031, 0.104, 0.65, "explosive"),
    ("スマホ冷却クーラー",      "ガジェット", 4,   36_000, 0.024, 0.088, 0.60, "steady"),
    ("折りたたみ水切りラック",  "ホーム",   46,   52_000, 0.014, 0.071, 0.45, "decel"),
    ("シートマスク 30枚",       "美容",   210,   28_000, 0.009, 0.052, 0.35, "decline"),
    ("電動鼻毛カッター",        "美容家電", 11,   64_000, 0.027, 0.096, 0.70, "steady"),
    ("珪藻土バスマット",        "ホーム",   62,   31_000, 0.011, 0.060, 0.40, "flat"),
    ("低温調理器 BONIQ",        "家電",      3,   19_000, 0.021, 0.074, 0.55, "flat"),
    ("首掛け扇風機",            "ガジェット",29,   47_000, 0.016, 0.068, 0.42, "decel"),
    ("マグネット式 収納ラック", "ホーム",    5,   14_000, 0.029, 0.101, 0.50, "surge"),
    # まぐれ 1 本型: 合計再生は多いが中央値も再現性も低い
    ("推し活 アクスタケース",   "ホビー",    9,    6_000, 0.006, 0.044, 0.12, "steady"),
]

CREATORS = [
    ("kaimono_yuka", "ゆか｜買ってよかった", 128_000),
    ("gadget_ken", "ケン / ガジェット", 74_000),
    ("kurashi_mio", "みお｜暮らしのモノ", 212_000),
    ("cosme_rin", "りん コスメ正直レビュー", 96_000),
    ("benri_taro", "便利グッズ太郎", 41_000),
    ("mama_life_a", "あや｜時短ママ", 63_000),
    ("review_jp", "レビュー研究所", 305_000),
]

BASE_TAGS = ["購入品紹介", "買ってよかった", "正直レビュー", "便利グッズ",
             "おすすめ", "神アイテム", "時短", "暮らしを整える"]

CATEGORY_TAGS = {
    "生活": ["暮らし", "掃除グッズ"], "美容": ["美容", "コスメ", "スキンケア"],
    "ガジェット": ["ガジェット", "ハック"], "ホーム": ["インテリア", "収納"],
    "美容家電": ["美容家電", "セルフケア"], "家電": ["家電", "時短家電"],
    "ホビー": ["推し活", "ヲタ活"],
}

CAPTIONS = [
    "【購入品紹介】{p}が想像以上だった",
    "正直レビュー: {p} 使って1週間",
    "これ買ってよかった…{p}",
    "{p}、まじで時短になる",
    "【本音】{p}は買いなのか",
    "リピート確定した{p}",
]


def _shape_multiplier(shape: str, day: float) -> float:
    """経過日数に応じた倍率. day が大きいほど新しい.

    倍率は 1 週間で高々 2-3 倍に収まるよう調整してある。
    指数を大きくすると中央値再生数が数百万まで膨らみ、
    実際の商品紹介動画のスケールから外れてしまうため。
    """
    if shape == "surge":
        # 直近で変曲する形。指数の *肩* が上がるので伸び率そのものが加速する。
        # 一定率の指数成長は「上昇中」であって「伸び始め」ではないため、
        # 加速フェーズを再現するにはこの形が要る。
        return math.exp(0.035 * day * day)
    if shape == "explosive":
        return math.exp(0.16 * day)      # 7日で約3.1倍
    if shape == "steady":
        return 1.0 + 0.06 * day          # 7日で約1.4倍
    if shape == "decel":
        return 1.0 + 0.30 * math.log1p(day * 1.2)
    if shape == "decline":
        return math.exp(-0.075 * day)    # 7日で約0.6倍
    return 1.0 + 0.008 * day


def _vid_id(product: str, idx: int) -> str:
    h = hashlib.sha1(f"{product}:{idx}".encode()).hexdigest()[:12]
    return str(int(h, 16))[:19]


@register("demo")
class DemoCollector(Collector):
    """決定論的な擬似『商品紹介動画』を生成する (ネットワーク不要).

    商品・クリエイター・ハッシュタグは生成しない。
    本番と同じく rollup が動画から導出する。
    """

    provides = (EntityType.VIDEO,)
    requires = "なし (オフライン)"

    def __init__(self, config: Any, day_offset: float = 0.0, seed: int = 42):
        super().__init__(config)
        #: 何日前のデータとして生成するか (履歴のバックフィルに使う)
        self.day_offset = day_offset
        self.seed = seed

    def collect(self, region: str) -> list[Snapshot]:
        captured = time.time() - self.day_offset * 86400
        day = max(0.0, 7.0 - self.day_offset)
        out: list[Snapshot] = []

        for pi, (name, cat, n_vid, base_v, save_r, eng_r, repro, shape) in enumerate(
                PRODUCT_SEEDS):
            mult = _shape_multiplier(shape, day)
            # 商品ごとに固定の乱数列 -> 同じ動画が日をまたいで一貫して育つ
            for i in range(n_vid):
                rng = random.Random(self.seed * 7919 + pi * 131 + i)
                # 再現性が高い商品ほど、各動画の再生数のばらつきが小さい
                # 再生数は対数正規に近い分布。再現性が低い商品ほど裾が長い
                spread = 1.0 - repro
                factor = math.exp(rng.gauss(-0.25, 0.45 + spread * 1.5))
                views = base_v * mult * factor
                # 再現性の低い商品は 1 本だけ突出させる (まぐれ型の再現)
                if repro < 0.3 and i == 0:
                    views *= 40.0

                views = max(300.0, views * rng.uniform(0.97, 1.03))
                likes = views * eng_r * rng.uniform(0.55, 0.75)
                saves = views * save_r * rng.uniform(0.85, 1.15)
                comments = views * eng_r * rng.uniform(0.03, 0.07)
                shares = views * eng_r * rng.uniform(0.08, 0.16)

                handle, nick, followers = CREATORS[(pi * 3 + i) % len(CREATORS)]
                age_h = max(2.0, rng.uniform(6, 26 * 24) - self.day_offset * 24 * 0)
                tags = ([BASE_TAGS[(pi + i) % len(BASE_TAGS)],
                         BASE_TAGS[(pi + i + 3) % len(BASE_TAGS)]]
                        + CATEGORY_TAGS.get(cat, []))

                metrics = {
                    M.VIEWS: round(views), M.LIKES: round(likes),
                    M.SAVES: round(saves), M.COMMENTS: round(comments),
                    M.SHARES: round(shares),
                    M.DURATION: round(rng.uniform(15, 62)),
                    M.AGE_HOURS: round(age_h, 1),
                    M.VELOCITY: views / age_h,
                    M.ENGAGEMENT_RATE: (likes + saves + comments + shares) / views,
                    M.SAVE_RATE: saves / views,
                }
                vid = _vid_id(name, i)
                # 実データでは商品リンクが付いていない紹介動画の方が多い。
                # 全部リンク付きにするとフィルタの経路をテストできないので、
                # 3 本に 1 本はリンク無し (キャプション判定のみ) にする。
                linked = (pi + i) % 3 != 0
                caption = CAPTIONS[(pi + i) % len(CAPTIONS)].format(p=name)
                out.append(Snapshot(
                    entity_type=EntityType.VIDEO,
                    native_id=vid,
                    name=caption,
                    source=self.name,
                    metrics=metrics,
                    region=region,
                    category=cat,
                    url=f"https://www.tiktok.com/@{handle}/video/{vid}",
                    thumbnail=None,   # デモでは実画像を持たない (UI 側で代替表示)
                    extra={
                        "creator": handle, "creator_name": nick,
                        "creator_followers": followers,
                        "hashtags": tags,
                        "product": ({"name": name, "url": None, "anchor_type": 2}
                                    if linked else None),
                        "product_intent": 1.0 if linked else 0.65,
                        "intent_words": ([] if linked else ["購入", "レビュー"]),
                        "query": "購入品紹介",
                        "create_time": captured - age_h * 3600,
                        "music": None,
                    },
                    captured_at=captured,
                ))
        return out
