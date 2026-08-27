"""TikTok 本体から「商品紹介系の動画」を収集する collector.

これが本システムの主力。
Creative Center は *全体の* トレンドしか見せてくれないが、
「どの商品で動画を撮るか」を決めるのに本当に必要なのは
**実際に伸びている商品紹介動画そのもの** だから。

やっていること
--------------
1. 検索キーワード / ハッシュタグごとに tiktok.com のページを実ブラウザで開く
2. ページ自身が発行する ``item_list`` 系の XHR レスポンス (JSON) を傍受する
3. 動画・投稿者・商品アンカーを取り出して Snapshot にする

なぜ HTTP 直叩きではなくブラウザなのか
--------------------------------------
tiktok.com の web API は ``msToken`` / ``X-Bogus`` / ``X-Gnarly`` といった
署名パラメータを要求し、その生成ロジックは難読化されたうえ頻繁に変わる。
自前で署名を再現する方式は必ず壊れる。
ブラウザに本物のページを開かせれば **署名は TikTok 自身が作る** ので、
仕様が変わっても動き続ける。遅いが、これが唯一まともに保守できる方法。

注意
----
- 公開されている情報のみを、個人の分析目的で取得する前提
- 既定でページ間に待機を入れている。間隔を詰めすぎないこと
- ログインが必要な範囲まで見たい場合のみ TIKTOK_SESSION_COOKIE を使う
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable
from urllib.parse import quote

from ..models import EntityType, M, Snapshot
from ..util.log import get
from .base import Collector, dedupe, register

log = get(__name__)

#: 商品紹介動画を探しに行く既定のクエリ (config で上書き可能)
DEFAULT_QUERIES: list[str] = [
    "購入品紹介", "買ってよかった", "正直レビュー", "おすすめ商品",
    "便利グッズ", "神アイテム", "レビュー", "開封",
]

#: 傍受対象の XHR。ここに出てくるものが動画リストを含む
ITEM_LIST_MARKERS = (
    "/api/search/general/full", "/api/search/item", "/api/challenge/item_list",
    "/api/post/item_list", "/api/recommend/item_list", "/api/explore/item_list",
    "/api/search/general/preview",
)

#: 商品紹介っぽさを判定するための語 (キャプション/ハッシュタグに対して)
PRODUCT_INTENT_WORDS = (
    "購入", "買っ", "レビュー", "紹介", "おすすめ", "開封", "使ってみた",
    "コスパ", "神アイテム", "便利", "リピート", "比較", "本音", "正直",
    "pr", "ad", "商品", "アイテム", "セール", "割引", "クーポン",
)

#: TikTok Shop リンクを含むアンカー種別
SHOP_ANCHOR_TYPES = {2, 46, 47}


def _walk_dicts(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    """JSON を再帰的に辿って dict を全部吐く (構造変化に強くするため)."""
    if depth > 8:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v, depth + 1)


def find_video_items(payload: Any) -> list[dict[str, Any]]:
    """レスポンスから動画アイテムらしき dict を集める.

    TikTok は ``itemList`` / ``data[].item`` / ``item_list`` など
    場所を変えてくるので、キー名ではなく **形** で判定する:
    「id を持ち、stats か statsV2 を持つ dict」= 動画アイテム。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in _walk_dicts(payload):
        if not isinstance(d, dict):
            continue
        has_stats = isinstance(d.get("stats"), dict) or isinstance(d.get("statsV2"), dict)
        vid = d.get("id") or d.get("itemId") or d.get("aweme_id")
        if has_stats and vid and str(vid) not in seen:
            seen.add(str(vid))
            out.append(d)
    return out


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def extract_hashtags(item: dict[str, Any]) -> list[str]:
    """textExtra とキャプション本文の両方からハッシュタグを拾う."""
    tags: list[str] = []
    for te in item.get("textExtra") or []:
        if isinstance(te, dict):
            name = te.get("hashtagName") or te.get("hashtag_name")
            if name:
                tags.append(str(name).lstrip("#"))
    desc = item.get("desc") or item.get("description") or ""
    for m in re.findall(r"#([^\s#＃]+)", str(desc)):
        if m not in tags:
            tags.append(m)
    return tags


def extract_product_anchor(item: dict[str, Any]) -> dict[str, Any] | None:
    """動画に紐づく TikTok Shop 商品アンカーを取り出す.

    商品リンクが付いている動画は「その商品の紹介動画」であることが確定するので、
    商品トレンドを組み立てる際の最も信頼できる根拠になる。
    """
    for key in ("anchors", "anchor", "anchorInfo", "shopInfo"):
        anchors = item.get(key)
        if isinstance(anchors, dict):
            anchors = [anchors]
        if not isinstance(anchors, list):
            continue
        for a in anchors:
            if not isinstance(a, dict):
                continue
            atype = a.get("type") or a.get("anchorType")
            keyword = (a.get("keyword") or a.get("title")
                       or a.get("description") or a.get("name"))
            if not keyword:
                continue
            try:
                is_shop = int(atype) in SHOP_ANCHOR_TYPES
            except (TypeError, ValueError):
                is_shop = False
            # 種別が判定できなくても、URL に shop が入っていれば商品とみなす
            url = str(a.get("schema") or a.get("url") or a.get("link") or "")
            if is_shop or "shop" in url.lower() or "product" in url.lower():
                return {"name": str(keyword).strip(), "url": url or None,
                        "anchor_type": atype}
    return None


def product_intent_score(desc: str, hashtags: list[str],
                         has_anchor: bool) -> float:
    """この動画がどれだけ「商品紹介動画」らしいか (0-1).

    商品リンク付きは確定 (1.0)。それ以外はキャプションとタグの語で推定する。
    無関係な動画をトレンド集計に混ぜないためのフィルタ。
    """
    if has_anchor:
        return 1.0
    hay = (desc + " " + " ".join(hashtags)).lower()
    hits = sum(1 for w in PRODUCT_INTENT_WORDS if w in hay)
    if hits == 0:
        return 0.0
    return min(1.0, 0.35 + 0.15 * hits)


def parse_item(item: dict[str, Any], region: str, source: str,
               query: str | None = None) -> Snapshot | None:
    """TikTok の動画アイテム 1 件を Snapshot にする."""
    vid = item.get("id") or item.get("itemId") or item.get("aweme_id")
    if not vid:
        return None

    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    stats2 = item.get("statsV2") if isinstance(item.get("statsV2"), dict) else {}

    def stat(*keys: str) -> float | None:
        for k in keys:
            v = _num(stats.get(k))
            if v is None:
                v = _num(stats2.get(k))
            if v is not None:
                return v
        return None

    views = stat("playCount", "play_count")
    likes = stat("diggCount", "digg_count")
    comments = stat("commentCount", "comment_count")
    shares = stat("shareCount", "share_count")
    saves = stat("collectCount", "collect_count")

    metrics: dict[str, float] = {}
    for k, v in ((M.VIEWS, views), (M.LIKES, likes), (M.COMMENTS, comments),
                 (M.SHARES, shares), (M.SAVES, saves)):
        if v is not None:
            metrics[k] = v

    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = author.get("uniqueId") or author.get("unique_id") or ""
    nickname = author.get("nickname") or handle

    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    duration = _num(video.get("duration")) or _num(item.get("duration"))
    if duration:
        # ミリ秒で来る場合がある
        metrics[M.DURATION] = duration / 1000.0 if duration > 600 else duration

    create_time = _num(item.get("createTime") or item.get("create_time"))
    now = time.time()
    if create_time and create_time > 1_000_000_000:
        age_h = max(0.5, (now - create_time) / 3600.0)
        metrics[M.AGE_HOURS] = age_h
        if views:
            # 時速。新しい動画と古い動画を公平に比べるための正規化
            metrics[M.VELOCITY] = views / age_h

    if views and views > 0:
        eng = sum(metrics.get(k, 0.0) for k in (M.LIKES, M.COMMENTS, M.SHARES, M.SAVES))
        if eng > 0:
            metrics[M.ENGAGEMENT_RATE] = eng / views
        if saves:
            metrics[M.SAVE_RATE] = saves / views

    if not metrics:
        return None

    desc = str(item.get("desc") or item.get("description") or "")
    hashtags = extract_hashtags(item)
    anchor = extract_product_anchor(item)
    intent = product_intent_score(desc, hashtags, anchor is not None)

    cover = (video.get("cover") or video.get("originCover")
             or video.get("dynamicCover") or item.get("cover"))

    return Snapshot(
        entity_type=EntityType.VIDEO,
        native_id=str(vid),
        name=(desc.strip() or f"@{handle} の動画")[:160],
        source=source,
        metrics=metrics,
        region=region,
        category=(hashtags[0] if hashtags else None),
        url=(f"https://www.tiktok.com/@{handle}/video/{vid}" if handle else None),
        thumbnail=str(cover) if cover else None,
        extra={
            "creator": handle,
            "creator_name": nickname,
            "creator_followers": _num((author.get("followerCount")
                                       if isinstance(author, dict) else None)),
            "hashtags": hashtags,
            "product": anchor,
            "product_intent": round(intent, 2),
            "query": query,
            "create_time": create_time,
            "music": ((item.get("music") or {}).get("title")
                      if isinstance(item.get("music"), dict) else None),
        },
    )


@register("tiktok_video")
class TikTokVideoCollector(Collector):
    """tiktok.com を実ブラウザで開き、商品紹介動画を収集する."""

    provides = (EntityType.VIDEO,)
    requires = "playwright + chromium (pip install playwright && playwright install chromium)"

    #: ページを開いてから XHR を待つ時間 (ミリ秒)
    settle_ms = 4500

    def available(self) -> tuple[bool, str]:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False, "playwright 未インストール (pip install playwright)"
        return True, "ok"

    # ------------------------------------------------------------------ 収集
    def queries(self) -> list[str]:
        raw = self.config.raw.get("video_queries")
        if isinstance(raw, list) and raw:
            return [str(q) for q in raw]
        return list(DEFAULT_QUERIES)

    def hashtags(self) -> list[str]:
        raw = self.config.raw.get("video_hashtags")
        return [str(h).lstrip("#") for h in raw] if isinstance(raw, list) else []

    def collect(self, region: str) -> list[Snapshot]:
        from playwright.sync_api import sync_playwright

        targets: list[tuple[str, str]] = []
        for q in self.queries():
            targets.append((q, f"https://www.tiktok.com/search/video?q={quote(q)}"))
        for h in self.hashtags():
            targets.append((f"#{h}", f"https://www.tiktok.com/tag/{quote(h)}"))
        if not targets:
            return []

        captured: list[dict[str, Any]] = []
        captured_at = time.time()
        out: list[Snapshot] = []
        # 0 件だったときに原因を示せるよう、見かけた API 通信を控えておく
        other_api: dict[str, int] = {}
        page_hint = ""

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.config.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                locale="ja-JP", timezone_id="Asia/Tokyo",
                viewport={"width": 1400, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            if self.config.tiktok_session_cookie:
                ctx.add_cookies([{
                    "name": "sessionid", "value": self.config.tiktok_session_cookie,
                    "domain": ".tiktok.com", "path": "/",
                }])
            page = ctx.new_page()
            current: dict[str, str] = {"q": ""}

            def on_response(resp: Any) -> None:
                url = resp.url
                if not any(m in url for m in ITEM_LIST_MARKERS):
                    # 傍受対象外でも API らしきものは控える (原因調査用)
                    if "/api/" in url:
                        key = url.split("?")[0]
                        other_api[key] = other_api.get(key, 0) + 1
                    return
                if resp.status != 200:
                    other_api[f"[{resp.status}] {url.split('?')[0]}"] = 1
                    return
                try:
                    if "json" not in (resp.headers or {}).get("content-type", ""):
                        return
                    payload = resp.json()
                except Exception:
                    return
                items = find_video_items(payload)
                for it in items:
                    it["__query"] = current["q"]
                captured.extend(items)
                if items:
                    log.debug("傍受: %d 件 (%s)", len(items), url[:70])

            page.on("response", on_response)

            for label, url in targets:
                current["q"] = label
                try:
                    log.info("動画を収集中: %s", label)
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(self.settle_ms)
                    self._scroll(page)
                except Exception as e:  # 1 クエリの失敗で全体を止めない
                    log.warning("%s の取得に失敗: %s", label, e)
                    continue
                if not captured and not page_hint:
                    # 何も取れていないときだけ、画面に何が出ているか読む
                    try:
                        page_hint = " ".join(page.inner_text("body")[:300].split())
                    except Exception:
                        pass

            ctx.close()
            browser.close()

        min_intent = float(self.config.raw.get("min_product_intent", 0.35))
        skipped = 0
        for raw in captured:
            try:
                snap = parse_item(raw, region, self.name, raw.get("__query"))
            except Exception:
                continue
            if snap is None:
                continue
            if float(snap.extra.get("product_intent") or 0) < min_intent:
                skipped += 1
                continue
            snap.captured_at = captured_at
            out.append(snap)

        log.info("動画 %d 件を採用 (商品紹介らしさ %.2f 未満の %d 件を除外)",
                 len(out), min_intent, skipped)

        if not captured:
            # ここが 0 だと、フィルタ以前に一覧そのものを受け取れていない。
            # 推測させないために、実際に見えたものを出す。
            log.warning("動画一覧の通信を1件も受け取れませんでした。")
            if page_hint:
                log.warning("  画面の文言: %s", page_hint[:200])
            if other_api:
                log.warning("  代わりに見えた API 通信 (上位10件):")
                for u, c in sorted(other_api.items(), key=lambda x: -x[1])[:10]:
                    log.warning("    %3d回  %s", c, u)
            else:
                log.warning("  API 通信自体がありませんでした "
                            "(ログイン壁・自動操作判定の可能性)")
            log.warning("  原因を詳しく調べるには: ttradar probe --visible")

        return dedupe(out)

    def _scroll(self, page: Any) -> None:
        """スクロールして追加の item_list をロードさせる."""
        rounds = max(1, min(int(self.config.limit_per_type / 12), 6))
        for _ in range(rounds):
            try:
                page.mouse.wheel(0, 3200)
                page.wait_for_timeout(1600)
            except Exception:
                break
