"""実ブラウザで Creative Center を開き、XHR レスポンスを傍受する collector.

なぜこれが最強か
----------------
Creative Center の API は署名パラメータ (``user-sign`` 等) や
必須ヘッダが不定期に変わる。HTTP で直叩きする方式はその都度壊れる。

対してこの方式は **TikTok 自身のフロントエンドにリクエストを作らせて、
返ってきた JSON を横から読むだけ** なので、署名仕様が変わっても動き続ける。
遅い代わりに壊れにくい。定期実行のメイン経路にはこちらを推奨する。

必要なもの
----------
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models import EntityType, Snapshot
from ..util.log import get
from .base import Collector, dedupe, find_list, register
from .creative_center import PARSERS

log = get(__name__)

CC_BASE = "https://ads.tiktok.com/business/creativecenter"

#: 画面パス -> そこで取れるエンティティ種別
PAGES: dict[EntityType, str] = {
    EntityType.HASHTAG: "inspiration/popular/hashtag/pc/en",
    EntityType.SONG: "inspiration/popular/music/pc/en",
    EntityType.VIDEO: "inspiration/topads/pc/en",
    EntityType.CREATOR: "inspiration/popular/creator/pc/en",
    EntityType.KEYWORD: "keyword-insights/pc/en",
}

#: URL に含まれる文字列 -> エンティティ種別 (傍受した XHR の振り分けに使う)
URL_MARKERS: list[tuple[str, EntityType]] = [
    ("hashtag", EntityType.HASHTAG),
    ("music", EntityType.SONG),
    ("song", EntityType.SONG),
    ("top_ads", EntityType.VIDEO),
    ("video", EntityType.VIDEO),
    ("product", EntityType.PRODUCT),
    ("keyword", EntityType.KEYWORD),
    ("creator", EntityType.CREATOR),
]


def classify_url(url: str) -> EntityType | None:
    """傍受した XHR の URL から、どのパーサーに渡すかを判定する."""
    low = url.lower()
    if "creative_radar_api" not in low and "creativecenter" not in low:
        return None
    # より具体的なマーカーを優先するため、パスの後ろから見る
    for marker, etype in URL_MARKERS:
        if marker in low:
            return etype
    return None


@register("browser_creative_center")
class BrowserCreativeCenterCollector(Collector):
    """Playwright で Creative Center を開き、XHR を傍受して収集する."""

    provides = (EntityType.HASHTAG, EntityType.SONG, EntityType.VIDEO,
                EntityType.KEYWORD, EntityType.CREATOR, EntityType.PRODUCT)
    requires = "playwright + chromium (pip install playwright && playwright install chromium)"

    def available(self) -> tuple[bool, str]:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False, "playwright 未インストール (pip install playwright)"
        return True, "ok"

    def collect(self, region: str) -> list[Snapshot]:
        from playwright.sync_api import sync_playwright

        want = {EntityType(t) for t in self.config.entity_types
                if t in {e.value for e in EntityType}}
        targets = {e: p for e, p in PAGES.items() if e in want}
        if not targets:
            return []

        captured_at = time.time()
        collected: list[Snapshot] = []
        # 傍受した生 JSON を種別ごとに溜める
        buckets: dict[EntityType, list[dict[str, Any]]] = {}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.config.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            if self.config.tiktok_session_cookie:
                # ログインが要る画面 (Shop 系) 用。任意。
                ctx.add_cookies([{
                    "name": "sessionid",
                    "value": self.config.tiktok_session_cookie,
                    "domain": ".tiktok.com",
                    "path": "/",
                }])

            page = ctx.new_page()

            def on_response(resp: Any) -> None:
                etype = classify_url(resp.url)
                if etype is None or resp.status != 200:
                    return
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" not in ctype:
                        return
                    payload = resp.json()
                except Exception:
                    return
                items = find_list(payload)
                if items:
                    buckets.setdefault(etype, []).extend(items)
                    log.debug("傍受: %s から %d 件 (%s)", etype.value, len(items), resp.url[:80])

            page.on("response", on_response)

            for etype, path in targets.items():
                url = f"{CC_BASE}/{path}?countryCode={region}&period={self.config.period_days}"
                try:
                    log.info("ブラウザで取得中: %s (%s)", etype.value, region)
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    # 初期 XHR の完了を待つ
                    page.wait_for_timeout(4_000)
                    self._scroll_to_load_more(page)
                except Exception as e:  # 1 ページの失敗で全体を止めない
                    log.warning("%s のページ取得に失敗: %s", etype.value, e)
                    continue

            ctx.close()
            browser.close()

        for etype, items in buckets.items():
            parser = PARSERS.get(etype)
            if parser is None:
                continue
            for raw in items:
                try:
                    snap = parser(raw, region, self.name)
                except Exception:
                    continue
                if snap and snap.metrics:
                    snap.captured_at = captured_at
                    collected.append(snap)

        return dedupe(collected)

    def _scroll_to_load_more(self, page: Any) -> None:
        """無限スクロール / もっと見るボタンを押して追加ロードを誘発する."""
        target_rounds = max(1, self.config.limit_per_type // 20)
        for _ in range(min(target_rounds, 5)):
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
                for label in ("View More", "もっと見る", "See more", "Load more"):
                    btn = page.query_selector(f"text={label}")
                    if btn:
                        btn.click(timeout=3000)
                        page.wait_for_timeout(2000)
                        break
            except Exception:
                break
