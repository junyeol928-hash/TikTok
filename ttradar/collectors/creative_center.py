"""TikTok Creative Center collector (HTTP 直叩き).

Creative Center (ads.tiktok.com/business/creativecenter) は TikTok 公式が
無料公開しているトレンド分析ツールで、ログイン無しで
「急上昇ハッシュタグ / 楽曲 / 人気動画 / 人気商品 / キーワード」が見られる。
その画面が内部で叩いている JSON API をここから直接呼ぶ。

重要な前提
----------
これは *公式ドキュメントのある API ではない* ため、パラメータ名やパスが
予告なく変わる。そのため:

- エンティティ種別ごとに **複数の候補エンドポイント** を持ち、順に試す
- レスポンスのキー名は :func:`pluck` で総当たりし、構造変化に耐える
- それでも全滅したら :mod:`ttradar.collectors.browser` (実ブラウザで XHR を
  傍受する方式) にフォールバックする。ブラウザ方式は署名やパラメータを
  TikTok 自身のフロントエンドに作らせるので、仕様変更に最も強い。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..models import EntityType, M, Snapshot
from ..util.log import get
from .base import Collector, dedupe, find_list, pluck, pluck_count, register

log = get(__name__)

API_BASE = "https://ads.tiktok.com/creative_radar_api/v1"
REFERER = "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en"

#: Creative Center が受け付ける集計期間
VALID_PERIODS = (7, 30, 120)


@dataclass
class EndpointSpec:
    """1 エンティティ種別に対する候補エンドポイント."""

    path: str
    params: dict[str, Any]
    list_hints: tuple[str, ...] = ()


def _period(days: int) -> int:
    """設定値を Creative Center が受け付ける値に丸める."""
    return min(VALID_PERIODS, key=lambda p: abs(p - days))


class CreativeCenterEndpoints:
    """種別ごとの候補エンドポイント定義."""

    @staticmethod
    def hashtag(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period,
                "country_code": region, "sort_by": "popular"}
        if industry:
            base["industry_id"] = industry
        return [
            EndpointSpec("popular_trend/hashtag/list", base,
                         ("data.list", "data.hashtag_list")),
            EndpointSpec("popular_trend/hashtag/list",
                         {**base, "sort_by": "trending"}, ("data.list",)),
            EndpointSpec("trending/hashtag/list", base, ("data.list",)),
        ]

    @staticmethod
    def song(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period,
                "country_code": region, "rank_type": "popular"}
        return [
            EndpointSpec("popular_trend/music/list", base,
                         ("data.sound_list", "data.list", "data.music_list")),
            EndpointSpec("popular_trend/song/list", base, ("data.list",)),
            # new_on_board=1 は「今週入ってきた新曲」= 最も早い段階のシグナル
            EndpointSpec("popular_trend/music/list",
                         {**base, "new_on_board": 1}, ("data.sound_list", "data.list")),
        ]

    @staticmethod
    def video(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period,
                "country_code": region, "order_by": "for_you"}
        if industry:
            base["industry"] = industry
        return [
            EndpointSpec("top_ads/v2/list", base, ("data.materials", "data.list")),
            EndpointSpec("top_ads/list", base, ("data.materials", "data.list")),
            EndpointSpec("popular_trend/video/list", base, ("data.list",)),
        ]

    @staticmethod
    def product(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period, "country_code": region}
        return [
            EndpointSpec("popular_trend/product/list", base,
                         ("data.list", "data.product_list")),
            EndpointSpec("top_products/list", base, ("data.list", "data.products")),
            EndpointSpec("commerce/product/list", base, ("data.list",)),
        ]

    @staticmethod
    def keyword(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period, "country_code": region}
        return [
            EndpointSpec("keyword/list", base, ("data.list", "data.keyword_list")),
            EndpointSpec("keyword_insights/list", base, ("data.list",)),
        ]

    @staticmethod
    def creator(region: str, limit: int, period: int, industry: str | None) -> list[EndpointSpec]:
        base = {"page": 1, "limit": limit, "period": period, "country_code": region}
        return [
            EndpointSpec("popular_trend/creator/list", base,
                         ("data.list", "data.creator_list")),
            EndpointSpec("creator/list", base, ("data.list",)),
        ]


SPEC_BUILDERS = {
    EntityType.HASHTAG: CreativeCenterEndpoints.hashtag,
    EntityType.SONG: CreativeCenterEndpoints.song,
    EntityType.VIDEO: CreativeCenterEndpoints.video,
    EntityType.PRODUCT: CreativeCenterEndpoints.product,
    EntityType.KEYWORD: CreativeCenterEndpoints.keyword,
    EntityType.CREATOR: CreativeCenterEndpoints.creator,
}


# ------------------------------------------------------------------ パーサー群
# レスポンスの 1 要素 -> Snapshot。キー名は総当たりする。

def parse_hashtag(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    name = pluck(item, "hashtag_name", "hashtag", "name", "title")
    if not name:
        return None
    name = str(name).lstrip("#")
    metrics: dict[str, float] = {}
    for key, cands in {
        M.POSTS: ("publish_cnt", "post_count", "video_count", "publish_count", "cnt"),
        M.VIEWS: ("video_views", "view_count", "views", "play_count"),
        M.RANK: ("rank", "rank_index", "index"),
        M.RELATED_CREATORS: ("creators", "creator_count", "influencer_cnt"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    # trend 配列 (日次の推移) があれば伸び率の初期値として使える
    trend = pluck(item, "trend", "trend_list")
    extra: dict[str, Any] = {}
    if isinstance(trend, list) and trend:
        pts = []
        for t in trend:
            if isinstance(t, dict):
                val = pluck_count(t, "value", "publish_cnt", "count")
                if val is not None:
                    pts.append(val)
        if pts:
            extra["trend_points"] = pts
    industry = pluck(item, "industry_info.value", "industry", "industry_name")
    return Snapshot(
        entity_type=EntityType.HASHTAG,
        native_id=name,
        name=f"#{name}",
        source=source,
        metrics=metrics,
        region=region,
        category=str(industry) if industry else None,
        url=f"https://www.tiktok.com/tag/{name}",
        extra=extra,
    )


def parse_song(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    title = pluck(item, "title", "song_name", "music_name", "name")
    if not title:
        return None
    song_id = pluck(item, "clip_id", "song_id", "music_id", "id", default=title)
    metrics: dict[str, float] = {}
    for key, cands in {
        M.VIEWS: ("post_change", "user_count", "play_count", "views", "video_count"),
        M.POSTS: ("video_count", "post_count", "usage_count"),
        M.RANK: ("rank", "rank_index"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    author = pluck(item, "author", "author_name", "artist")
    link = pluck(item, "song_url", "link", "share_url")
    return Snapshot(
        entity_type=EntityType.SONG,
        native_id=str(song_id),
        name=f"{title}" + (f" / {author}" if author else ""),
        source=source,
        metrics=metrics,
        region=region,
        url=str(link) if link else None,
        thumbnail=pluck(item, "cover", "cover_url", "album_cover"),
        extra={"author": author, "title": title},
    )


def parse_video(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    vid = pluck(item, "id", "video_id", "item_id", "ad_id")
    if not vid:
        return None
    title = pluck(item, "brand_name", "title", "desc", "ad_title", default=str(vid))
    metrics: dict[str, float] = {}
    for key, cands in {
        M.VIEWS: ("video_views", "play_count", "views", "impression"),
        M.LIKES: ("like", "likes", "digg_count", "like_count"),
        M.COMMENTS: ("comment", "comments", "comment_count"),
        M.SHARES: ("share", "shares", "share_count"),
        M.CTR: ("ctr",),
        M.RANK: ("rank", "rank_index"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    # エンゲージメント率は「型の良さ」の指標。再生数だけ多い動画より参考になる
    views = metrics.get(M.VIEWS)
    if views and views > 0:
        eng = sum(metrics.get(k, 0.0) for k in (M.LIKES, M.COMMENTS, M.SHARES))
        if eng > 0:
            metrics[M.ENGAGEMENT_RATE] = eng / views
    return Snapshot(
        entity_type=EntityType.VIDEO,
        native_id=str(vid),
        name=str(title)[:120],
        source=source,
        metrics=metrics,
        region=region,
        category=pluck(item, "industry_key", "objective_key", "industry"),
        url=pluck(item, "video_url", "share_url", "link"),
        thumbnail=pluck(item, "cover", "video_info.cover", "cover_url"),
        extra={"duration": pluck_count(item, "video_info.duration", "duration")},
    )


def parse_product(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    pid = pluck(item, "product_id", "id", "item_id")
    title = pluck(item, "product_name", "title", "name")
    if not (pid or title):
        return None
    metrics: dict[str, float] = {}
    for key, cands in {
        M.SALES: ("sales", "sold_count", "sale_cnt", "order_count", "sales_count"),
        M.REVENUE: ("revenue", "gmv", "sales_amount"),
        M.PRICE: ("price", "min_price", "sale_price", "avg_price"),
        M.RATING: ("rating", "score", "star"),
        M.COMMISSION_RATE: ("commission_rate", "commission", "rate"),
        M.RELATED_VIDEOS: ("video_count", "related_video_cnt", "video_cnt"),
        M.RELATED_CREATORS: ("creator_count", "influencer_cnt"),
        M.RANK: ("rank", "rank_index"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    # 報酬率が 15 のような整数で来た場合は % とみなして 0-1 に正規化
    cr = metrics.get(M.COMMISSION_RATE)
    if cr is not None and cr > 1.0:
        metrics[M.COMMISSION_RATE] = cr / 100.0
    return Snapshot(
        entity_type=EntityType.PRODUCT,
        native_id=str(pid or title),
        name=str(title or pid)[:120],
        source=source,
        metrics=metrics,
        region=region,
        category=pluck(item, "category_name", "category", "first_category"),
        url=pluck(item, "product_url", "link", "share_url"),
        thumbnail=pluck(item, "cover", "image", "product_image"),
        extra={"shop": pluck(item, "shop_name", "seller_name")},
    )


def parse_keyword(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    kw = pluck(item, "keyword", "word", "name", "query")
    if not kw:
        return None
    metrics: dict[str, float] = {}
    for key, cands in {
        M.SEARCH_VOLUME: ("search_volume", "volume", "impression", "search_cnt"),
        M.CTR: ("ctr",),
        M.RANK: ("rank", "rank_index"),
        M.POSTS: ("video_count", "post_count"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    return Snapshot(
        entity_type=EntityType.KEYWORD,
        native_id=str(kw),
        name=str(kw),
        source=source,
        metrics=metrics,
        region=region,
        category=pluck(item, "industry", "category"),
        url=f"https://www.tiktok.com/search?q={kw}",
    )


def parse_creator(item: dict[str, Any], region: str, source: str) -> Snapshot | None:
    handle = pluck(item, "tcm_id", "unique_id", "user_id", "id", "nick_name")
    name = pluck(item, "nick_name", "nickname", "name", default=str(handle))
    if not handle:
        return None
    metrics: dict[str, float] = {}
    for key, cands in {
        M.FOLLOWERS: ("follower_cnt", "follower_count", "followers"),
        M.LIKES: ("liked_cnt", "like_count", "hearts"),
        M.POSTS: ("video_cnt", "video_count"),
        M.VIEWS: ("avg_views", "view_count"),
    }.items():
        v = pluck_count(item, *cands)
        if v is not None:
            metrics[key] = v
    uid = pluck(item, "unique_id", "tcm_id")
    return Snapshot(
        entity_type=EntityType.CREATOR,
        native_id=str(handle),
        name=str(name),
        source=source,
        metrics=metrics,
        region=region,
        url=f"https://www.tiktok.com/@{uid}" if uid else None,
        thumbnail=pluck(item, "avatar_url", "avatar"),
    )


PARSERS = {
    EntityType.HASHTAG: parse_hashtag,
    EntityType.SONG: parse_song,
    EntityType.VIDEO: parse_video,
    EntityType.PRODUCT: parse_product,
    EntityType.KEYWORD: parse_keyword,
    EntityType.CREATOR: parse_creator,
}


@register("creative_center")
class CreativeCenterCollector(Collector):
    """Creative Center の JSON API を直接叩く高速パス."""

    provides = (EntityType.HASHTAG, EntityType.SONG, EntityType.VIDEO,
                EntityType.PRODUCT, EntityType.KEYWORD, EntityType.CREATOR)
    requires = "なし (認証不要). ただし TikTok 側の仕様変更に弱い"

    def collect(self, region: str) -> list[Snapshot]:
        want = {EntityType(t) for t in self.config.entity_types
                if t in {e.value for e in EntityType}}
        period = _period(self.config.period_days)
        industries = self.config.industries or [None]
        out: list[Snapshot] = []
        captured = time.time()

        for etype in self.provides:
            if etype not in want:
                continue
            builder = SPEC_BUILDERS[etype]
            parser = PARSERS[etype]
            for industry in industries:
                specs = builder(region, self.config.limit_per_type, period, industry)
                items = self._try_specs(specs, etype)
                for raw in items:
                    try:
                        snap = parser(raw, region, self.name)
                    except Exception as e:  # 1 件の壊れたレコードで全部を落とさない
                        log.debug("%s のパースに失敗: %s", etype.value, e)
                        continue
                    if snap and snap.metrics:
                        snap.captured_at = captured
                        out.append(snap)
        return dedupe(out)

    def _try_specs(self, specs: list[EndpointSpec], etype: EntityType) -> list[dict[str, Any]]:
        """候補エンドポイントを順に試し、最初に成功したものを返す."""
        for spec in specs:
            url = f"{API_BASE}/{spec.path}"
            try:
                payload = self.http.get_json(url, params=spec.params, referer=REFERER)
            except Exception as e:
                log.debug("%s 失敗: %s", spec.path, e)
                continue
            # Creative Center は成功時 code=0
            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 0, "0"):
                    log.debug("%s が code=%s を返しました: %s",
                              spec.path, code, payload.get("msg"))
                    continue
            items = find_list(payload, *spec.list_hints)
            if items:
                log.debug("%s: %s から %d 件", etype.value, spec.path, len(items))
                return items
        log.warning(
            "%s: Creative Center の全候補エンドポイントが失敗しました。"
            "browser_creative_center へのフォールバックを検討してください", etype.value,
        )
        return []
