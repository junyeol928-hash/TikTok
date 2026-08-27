"""ttradar のコアデータモデル.

設計の要点
----------
TikTok のトレンドは「今どれだけ大きいか」より「どれだけ速く伸びているか」が重要。
そのため全てのエンティティ (ハッシュタグ / 商品 / 楽曲 / 動画 / クリエイター / キーワード)
を同じ形の *スナップショット* として時系列で貯め、後段で差分・伸び率を計算する。

- Entity   : 追跡対象そのもの (例: #購入品紹介 という 1 つのハッシュタグ)
- Snapshot : ある時刻の Entity の計測値 (例: 8/26 時点で投稿数 12,340 件)

collector は Snapshot を吐くだけ。伸び率の計算・スコアリングは analysis 側の責務。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class EntityType(str, Enum):
    """追跡対象の種別."""

    HASHTAG = "hashtag"       # ハッシュタグ
    SONG = "song"             # 楽曲 / サウンド
    PRODUCT = "product"       # TikTok Shop 商品
    VIDEO = "video"           # 動画 (トレンド動画 / Top Ads)
    CREATOR = "creator"       # クリエイター
    KEYWORD = "keyword"       # 検索キーワード


class TrendStage(str, Enum):
    """トレンドの段階.

    「今どのフェーズか」が投稿判断そのもの。
    NEW/EMERGING を拾えるかがこのシステムの存在価値。
    """

    NEW = "new"               # 初検出。まだ誰も気づいていない可能性
    EMERGING = "emerging"     # 伸び始め。加速度がプラス。★最も美味しい
    RISING = "rising"         # 順調に上昇中。まだ乗れる
    PEAKING = "peaking"       # 伸びが鈍化。飽和しつつある
    DECLINING = "declining"   # 下降。今から参入しても遅い
    STABLE = "stable"         # 横ばい。定番

    @property
    def label_ja(self) -> str:
        return {
            "new": "新規検出",
            "emerging": "伸び始め",
            "rising": "上昇中",
            "peaking": "ピーク",
            "declining": "下降",
            "stable": "横ばい",
        }[self.value]

    @property
    def emoji(self) -> str:
        return {
            "new": "🆕", "emerging": "🚀", "rising": "📈",
            "peaking": "⚠️", "declining": "📉", "stable": "➖",
        }[self.value]


# 正規化されたメトリクス名。collector が何を取ってきても最終的にこの名前に寄せる。
# ここを固定しておかないと、ソースが増えたときに分析側が破綻する。
class M:
    VIEWS = "views"                     # 再生数
    POSTS = "posts"                     # 投稿数
    LIKES = "likes"
    COMMENTS = "comments"
    SHARES = "shares"
    SAVES = "saves"                     # 保存数. 商品紹介では購買意欲の最重要シグナル
    FOLLOWERS = "followers"
    SALES = "sales"                     # 販売個数
    REVENUE = "revenue"                 # 売上金額
    PRICE = "price"                     # 価格
    RATING = "rating"                   # 評価
    COMMISSION_RATE = "commission_rate" # アフィリ報酬率 (0-1)
    RELATED_VIDEOS = "related_videos"   # 関連動画数 = 競合の多さ
    RELATED_CREATORS = "related_creators"
    SEARCH_VOLUME = "search_volume"
    RANK = "rank"                       # ランキング順位 (小さいほど上位)
    CTR = "ctr"
    ENGAGEMENT_RATE = "engagement_rate"
    SAVE_RATE = "save_rate"             # 保存率 = 保存数 / 再生数
    DURATION = "duration"               # 動画の長さ (秒)
    VIDEO_COUNT = "video_count"         # その商品を紹介している動画の本数
    CREATOR_COUNT = "creator_count"     # その商品を紹介しているクリエイター数
    TOTAL_VIEWS = "total_views"         # 紹介動画の合計再生数
    MEDIAN_VIEWS = "median_views"       # 紹介動画の再生数の中央値
    AGE_HOURS = "age_hours"             # 投稿からの経過時間
    VELOCITY = "velocity"               # 再生数 / 経過時間 (時速)
    HIT_RATE = "hit_rate"               # 全体中央値を超えた動画の割合 = 再現性


#: エンティティ種別ごとの「主要ボリューム指標」。伸び率はこの値で計算する。
PRIMARY_METRIC: dict[EntityType, str] = {
    EntityType.HASHTAG: M.POSTS,
    EntityType.SONG: M.VIEWS,
    EntityType.PRODUCT: M.SALES,
    EntityType.VIDEO: M.VIEWS,
    EntityType.CREATOR: M.FOLLOWERS,
    EntityType.KEYWORD: M.SEARCH_VOLUME,
}


def make_entity_key(entity_type: EntityType | str, source: str, native_id: str) -> str:
    """エンティティの安定した一意キーを作る.

    同じハッシュタグを別 collector が取ってきても同一視できるよう、
    native_id は正規化 (小文字化 / 前後空白除去) してからハッシュする。
    """
    et = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type)
    norm = str(native_id).strip().lower().lstrip("#")
    raw = f"{et}:{norm}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{et}_{digest}"


@dataclass
class Snapshot:
    """ある時刻における 1 エンティティの計測結果.

    collector が返す唯一の型。これ以外を返してはいけない。
    """

    entity_type: EntityType
    native_id: str                       # ソース上の ID (ハッシュタグ名 / 商品 ID 等)
    name: str                            # 表示名
    source: str                          # 取得元 collector 名
    metrics: dict[str, float] = field(default_factory=dict)
    region: str = "JP"
    category: str | None = None          # 業種 / 商品カテゴリ
    url: str | None = None
    thumbnail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # 生データの一部を保持
    captured_at: float = field(default_factory=time.time)

    @property
    def entity_key(self) -> str:
        return make_entity_key(self.entity_type, self.source, self.native_id)

    @property
    def primary_value(self) -> float | None:
        """このエンティティの主要ボリューム値 (伸び率計算の対象)."""
        key = PRIMARY_METRIC.get(self.entity_type)
        if key is None:
            return None
        val = self.metrics.get(key)
        # 主要指標が無い場合は、代替として views -> posts -> sales の順で拾う
        if val is None:
            for alt in (M.VIEWS, M.POSTS, M.SALES, M.SEARCH_VOLUME, M.FOLLOWERS):
                if alt in self.metrics:
                    return float(self.metrics[alt])
        return float(val) if val is not None else None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["entity_type"] = self.entity_type.value
        d["metrics"] = json.dumps(self.metrics, ensure_ascii=False)
        d["extra"] = json.dumps(self.extra, ensure_ascii=False, default=str)
        d["entity_key"] = self.entity_key
        d["primary_value"] = self.primary_value
        return d


@dataclass
class TrendSignal:
    """分析後のシグナル = 実際にユーザーが見る 1 行.

    「何を」「なぜ今」「どれくらい急いで」やるべきかが 1 つに収まっている。
    """

    entity_key: str
    entity_type: EntityType
    name: str
    source: str
    region: str
    stage: TrendStage
    score: float                          # 0-100 の総合スコア
    current_value: float | None           # 現在の主要指標値
    growth_rate: float | None             # 期間内の伸び率 (0.35 = +35%)
    acceleration: float | None            # 伸び率の変化 (加速度)
    window_hours: float | None            # 何時間分の比較か
    reasons: list[str] = field(default_factory=list)   # スコアの根拠 (日本語)
    metrics: dict[str, float] = field(default_factory=dict)
    category: str | None = None
    url: str | None = None
    thumbnail: str | None = None
    is_new: bool = False
    first_seen: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entity_type"] = self.entity_type.value
        d["stage"] = self.stage.value
        d["stage_label"] = self.stage.label_ja
        d["stage_emoji"] = self.stage.emoji
        return d
