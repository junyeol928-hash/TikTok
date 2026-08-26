"""オフライン検証用のデモ collector.

TikTok に到達できない環境 (CI / 制限付きネットワーク / 初回セットアップ確認) でも
パイプライン全体を動かせるようにするためのもの。

単なるランダムではなく **意図的に色々なトレンド形状を作る**:
爆発的に伸びるもの / 伸びが鈍化するもの / 下降するもの / 横ばいのもの。
これによりステージ判定とスコアリングが正しく機能しているか検証できる。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

from ..models import EntityType, M, Snapshot
from .base import Collector, register

#: (表示名, カテゴリ, 初期値, 形状)
#: 形状: explosive=爆発 / steady=順調 / decel=鈍化 / decline=下降 / flat=横ばい
HASHTAG_SEEDS: list[tuple[str, str, float, str]] = [
    ("購入品紹介", "ライフスタイル", 8_000, "steady"),
    ("バズりコスメ", "美容", 3_200, "explosive"),
    ("便利グッズ", "ホーム", 15_000, "decel"),
    ("ガチレビュー", "ライフスタイル", 1_100, "explosive"),
    ("韓国コスメ", "美容", 42_000, "flat"),
    ("時短家電", "家電", 900, "steady"),
    ("プチプラ", "美容", 66_000, "decline"),
    ("キッチン用品", "ホーム", 5_400, "steady"),
    ("推し活グッズ", "ホビー", 2_100, "explosive"),
    ("開封動画", "ライフスタイル", 30_000, "flat"),
]

PRODUCT_SEEDS: list[tuple[str, str, float, float, float, int, str]] = [
    # (商品名, カテゴリ, 販売数, 価格, 報酬率, 競合動画数, 形状)
    ("温感アイマスク 20枚入", "美容", 4_200, 1_280, 0.20, 34, "explosive"),
    ("折りたたみ水切りラック", "ホーム", 9_800, 2_480, 0.12, 210, "decel"),
    ("毛玉取り器 充電式", "生活", 1_500, 1_980, 0.25, 18, "explosive"),
    ("シートマスク 30枚", "美容", 22_000, 990, 0.15, 890, "decline"),
    ("スマホ冷却クーラー", "ガジェット", 800, 3_480, 0.22, 12, "steady"),
    ("珪藻土バスマット", "ホーム", 6_100, 2_980, 0.10, 320, "flat"),
    ("電動鼻毛カッター", "美容家電", 2_600, 1_680, 0.28, 45, "steady"),
    ("低温調理器", "家電", 430, 8_900, 0.08, 62, "flat"),
]

SONG_SEEDS = [
    ("キラキラ Vibes", "artist_a", 120_000, "explosive"),
    ("夏の終わりに", "artist_b", 540_000, "decel"),
    ("Neon Tokyo", "artist_c", 88_000, "steady"),
    ("しあわせのレシピ", "artist_d", 1_200_000, "flat"),
]

KEYWORD_SEEDS = [
    ("プチプラ 神コスメ", "美容", 44_000, "steady"),
    ("時短 便利グッズ", "ホーム", 12_000, "explosive"),
    ("これ買ってよかった", "ライフスタイル", 89_000, "decel"),
    ("正直レビュー", "ライフスタイル", 6_800, "explosive"),
]


def _shape_multiplier(shape: str, day: float) -> float:
    """経過日数に応じた倍率を返す. day=0 が最古, 大きいほど新しい."""
    if shape == "explosive":       # 指数的に加速
        return math.exp(0.35 * day)
    if shape == "steady":          # 一定率で成長
        return 1.0 + 0.12 * day
    if shape == "decel":           # 伸びるが頭打ち (対数)
        return 1.0 + 0.5 * math.log1p(day * 1.5)
    if shape == "decline":         # 減衰
        return math.exp(-0.10 * day)
    return 1.0 + 0.01 * day        # flat


@register("demo")
class DemoCollector(Collector):
    """決定論的な擬似データを生成する (ネットワーク不要)."""

    provides = (EntityType.HASHTAG, EntityType.PRODUCT,
                EntityType.SONG, EntityType.KEYWORD)
    requires = "なし (オフライン)"

    def __init__(self, config: Any, day_offset: float = 0.0, seed: int = 42):
        super().__init__(config)
        #: 何日前のデータとして生成するか (履歴のバックフィルに使う)
        self.day_offset = day_offset
        self.seed = seed

    def collect(self, region: str) -> list[Snapshot]:
        rng = random.Random(self.seed + int(self.day_offset * 100))
        captured = time.time() - self.day_offset * 86400
        # day_offset=0 が最新なので、形状計算では反転させる
        day = max(0.0, 7.0 - self.day_offset)
        out: list[Snapshot] = []

        def jitter(v: float) -> float:
            return max(0.0, v * rng.uniform(0.96, 1.04))

        for name, cat, base_v, shape in HASHTAG_SEEDS:
            posts = jitter(base_v * _shape_multiplier(shape, day))
            out.append(Snapshot(
                entity_type=EntityType.HASHTAG, native_id=name, name=f"#{name}",
                source=self.name,
                metrics={M.POSTS: round(posts),
                         M.VIEWS: round(posts * rng.uniform(180, 420)),
                         M.RELATED_CREATORS: round(posts * rng.uniform(0.15, 0.3))},
                region=region, category=cat,
                url=f"https://www.tiktok.com/tag/{name}",
                captured_at=captured,
            ))

        for name, cat, sales, price, comm, videos, shape in PRODUCT_SEEDS:
            s = jitter(sales * _shape_multiplier(shape, day))
            out.append(Snapshot(
                entity_type=EntityType.PRODUCT, native_id=name, name=name,
                source=self.name,
                metrics={M.SALES: round(s), M.REVENUE: round(s * price),
                         M.PRICE: price, M.COMMISSION_RATE: comm,
                         M.RATING: round(rng.uniform(4.1, 4.9), 1),
                         M.RELATED_VIDEOS: round(videos * (1 + 0.08 * day))},
                region=region, category=cat,
                captured_at=captured,
            ))

        for title, author, views, shape in SONG_SEEDS:
            v = jitter(views * _shape_multiplier(shape, day))
            out.append(Snapshot(
                entity_type=EntityType.SONG, native_id=title,
                name=f"{title} / {author}", source=self.name,
                metrics={M.VIEWS: round(v), M.POSTS: round(v / rng.uniform(20, 60))},
                region=region, captured_at=captured,
            ))

        for kw, cat, vol, shape in KEYWORD_SEEDS:
            v = jitter(vol * _shape_multiplier(shape, day))
            out.append(Snapshot(
                entity_type=EntityType.KEYWORD, native_id=kw, name=kw,
                source=self.name,
                metrics={M.SEARCH_VOLUME: round(v)},
                region=region, category=cat,
                url=f"https://www.tiktok.com/search?q={kw}",
                captured_at=captured,
            ))
        return out
