"""外部 TikTok Shop 分析サービスの汎用アダプタ.

背景
----
TikTok Shop の *実売データ* (販売個数・GMV・報酬率) を面で取れる公式の
無料経路は存在しない。本気でやるなら Kalodata / FastMoss / EchoTik /
Shoplus といった有料サービスの API を使うことになる。

各社の API 仕様は非公開かつバラバラなので、**コードにハードコードせず
config.yaml で定義を書ける汎用アダプタ** にしてある。
契約したサービスのドキュメントを見て、下記のように書けば動く::

    thirdparty_apis:
      - name: kalodata
        base_url: https://api.kalodata.com/v1
        path: /product/rank
        api_key_env: KALODATA_API_KEY
        auth: header            # header | query | bearer
        auth_name: X-API-KEY
        entity_type: product
        params:
          country: JP
          period: 7
        list_path: data.list    # 省略時は自動探索
        field_map:              # 先方のキー -> ttradar の正規化キー
          name: productName
          sales: salesCount
          revenue: gmv
          price: price
          commission_rate: commissionRate
          related_videos: videoCount
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..models import EntityType, M, Snapshot
from ..util.log import get
from .base import Collector, dedupe, find_list, parse_count, pluck, register

log = get(__name__)

#: field_map で使える正規化キー
MAPPABLE = {
    "name", "native_id", "url", "thumbnail", "category",
    M.SALES, M.REVENUE, M.PRICE, M.RATING, M.COMMISSION_RATE,
    M.RELATED_VIDEOS, M.RELATED_CREATORS, M.VIEWS, M.POSTS,
    M.SEARCH_VOLUME, M.FOLLOWERS, M.RANK,
}


@register("thirdparty")
class ThirdPartyCollector(Collector):
    """config.yaml の ``thirdparty_apis`` 定義に従って外部 API を叩く."""

    provides = (EntityType.PRODUCT, EntityType.VIDEO, EntityType.CREATOR,
                EntityType.HASHTAG, EntityType.KEYWORD)
    requires = "契約中サービスの API キー (config.yaml の thirdparty_apis に定義)"

    def _specs(self) -> list[dict[str, Any]]:
        return self.config.raw.get("thirdparty_apis") or []

    def available(self) -> tuple[bool, str]:
        specs = self._specs()
        if not specs:
            return False, "thirdparty_apis が未定義"
        usable = [s for s in specs if not s.get("api_key_env") or os.getenv(s["api_key_env"])]
        if not usable:
            envs = ", ".join(s.get("api_key_env", "?") for s in specs)
            return False, f"API キーが未設定 ({envs})"
        return True, f"{len(usable)} 件の API 定義"

    def collect(self, region: str) -> list[Snapshot]:
        out: list[Snapshot] = []
        captured = time.time()
        for spec in self._specs():
            key_env = spec.get("api_key_env")
            api_key = os.getenv(key_env) if key_env else None
            if key_env and not api_key:
                log.info("%s: %s が未設定のためスキップ", spec.get("name"), key_env)
                continue
            try:
                out.extend(self._call(spec, api_key, region, captured))
            except Exception as e:  # noqa: BLE001
                log.warning("外部API %s が失敗: %s", spec.get("name"), e)
        return dedupe(out)

    def _call(self, spec: dict[str, Any], api_key: str | None,
              region: str, captured: float) -> list[Snapshot]:
        base = spec["base_url"].rstrip("/")
        url = base + "/" + spec.get("path", "").lstrip("/")
        params = dict(spec.get("params") or {})
        headers: dict[str, str] = {}

        auth = (spec.get("auth") or "header").lower()
        auth_name = spec.get("auth_name") or "X-API-KEY"
        if api_key:
            if auth == "query":
                params[auth_name] = api_key
            elif auth == "bearer":
                headers["Authorization"] = f"Bearer {api_key}"
            else:
                headers[auth_name] = api_key

        payload = self.http.get_json(url, params=params, headers=headers)
        items = find_list(payload, *( [spec["list_path"]] if spec.get("list_path") else [] ))
        if not items:
            log.warning("%s: レスポンスからリストを抽出できませんでした", spec.get("name"))
            return []

        etype = EntityType(spec.get("entity_type", "product"))
        fmap: dict[str, str] = spec.get("field_map") or {}
        source_name = f"{self.name}:{spec.get('name', 'api')}"
        out: list[Snapshot] = []

        for raw in items:
            mapped: dict[str, Any] = {}
            for norm_key, their_key in fmap.items():
                if norm_key not in MAPPABLE:
                    continue
                mapped[norm_key] = pluck(raw, their_key)

            name = mapped.get("name") or pluck(raw, "name", "title", "product_name")
            native = mapped.get("native_id") or pluck(raw, "id", "product_id", default=name)
            if not name and not native:
                continue

            metrics: dict[str, float] = {}
            for mk in MAPPABLE:
                if mk in {"name", "native_id", "url", "thumbnail", "category"}:
                    continue
                val = parse_count(mapped.get(mk))
                if val is not None:
                    metrics[mk] = val
            if not metrics:
                continue
            cr = metrics.get(M.COMMISSION_RATE)
            if cr is not None and cr > 1.0:
                metrics[M.COMMISSION_RATE] = cr / 100.0

            out.append(Snapshot(
                entity_type=etype,
                native_id=str(native or name),
                name=str(name or native)[:120],
                source=source_name,
                metrics=metrics,
                region=region,
                category=mapped.get("category"),
                url=mapped.get("url"),
                thumbnail=mapped.get("thumbnail"),
                captured_at=captured,
            ))
        return out
