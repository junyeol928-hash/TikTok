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
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from ..analysis.category import is_food
from ..analysis.product_name import extract_products
from ..models import EntityType, M, Snapshot
from ..util.log import get
from .base import Collector, dedupe, register

log = get(__name__)

#: 商品紹介動画を探しに行く既定のクエリ (config で上書き可能)
DEFAULT_QUERIES: list[str] = [
    "購入品紹介", "買ってよかった", "正直レビュー", "おすすめ商品",
    "便利グッズ", "神アイテム", "レビュー", "開封",
]

#: 傍受対象の XHR。ここに出てくるものが動画リストを含む。
#: パスを決め打ちすると TikTok が置き場所を変えたときに静かに 0 件になる。
#: 実際 ``/api/prefetch/explore/item_list/`` は決め打ちの一覧から漏れていた。
#: 「item_list を含む」「検索系」という *形* で拾う。
_ITEM_LIST_RE = re.compile(
    r"(item_list|/api/search/(general|item)|/api/recommend/|"
    r"/api/challenge/|/api/post/|/aweme/v1/.*(feed|search))", re.I)


def is_item_list_url(url: str) -> bool:
    """動画リストを含みうる API 通信か."""
    return bool(_ITEM_LIST_RE.search(url or ""))

#: 商品紹介っぽさを判定するための語 (キャプション/ハッシュタグに対して)。
#: 日本語は語境界が無いので部分一致で判定する。
PRODUCT_INTENT_WORDS = (
    "購入", "買っ", "レビュー", "紹介", "おすすめ", "開封", "使ってみた",
    "コスパ", "神アイテム", "便利", "リピート", "比較", "本音", "正直",
    "商品", "アイテム", "セール", "割引", "クーポン",
)

#: 英字の目印は **語として** 一致させる。
#: 部分一致にすると "pr" が "spring"、"ad" が "made"/"ready"/"ADHD" に
#: 引っかかり、商品紹介ではない動画が丸ごと分析対象に混ざる。
PRODUCT_INTENT_TOKENS = (
    "pr", "ad", "review", "haul", "unboxing", "gifted", "tiktokmademebuyit",
)
_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(PRODUCT_INTENT_TOKENS) + r")(?![a-z0-9])")

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


#: 商品リンクらしい URL
_SHOP_URL_RE = re.compile(r"(/view/product|shop\.tiktok|/shop/|product_id|productId"
                          r"|\bproduct\b|tiktokshop)", re.I)
#: 商品名が入りうるキー
_ANCHOR_NAME_KEYS = ("keyword", "title", "product_name", "productName",
                     "name", "description")
#: 商品 ID が入りうるキー
_PRODUCT_ID_KEYS = ("product_id", "productId", "product_ids", "productIds")
#: アンカーらしさを示す構造キー (種別だけで判断すると誤検出するため)
_ANCHOR_SHAPE_KEYS = ("keyword", "schema", "icon", "extraInfo", "extra_info",
                      "actionType", "action_type", "logExtra", "anchorType")


def _anchor_name(d: dict[str, Any]) -> str | None:
    for k in _ANCHOR_NAME_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _anchor_url(d: dict[str, Any]) -> str:
    for k in ("schema", "url", "link", "deep_link", "deepLink", "webUrl", "web_url"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def extract_product_anchor(item: dict[str, Any]) -> dict[str, Any] | None:
    """動画に紐づく TikTok Shop 商品アンカーを取り出す.

    商品リンクが付いている動画は「その商品の紹介動画」であることが確定するので、
    商品トレンドを組み立てる際の最も信頼できる根拠になる。

    キー名ではなく **形** で探す。TikTok はアンカーの置き場所
    (``anchors`` / ``anchorInfo`` / ``shopInfo`` / さらに入れ子) を変えてくるため、
    決め打ちだと商品が 1 件も取れない状態に静かに陥る。

    「商品リンクだ」と判断する条件は、名前を持ったうえで次のいずれか:

    - 種別が Shop 系で、かつアンカーらしい構造キーを持つ
    - 商品 ID を持つ
    - URL が商品ページらしい

    種別だけで判断すると、``type: 2`` を持つ無関係な dict を拾ってしまう。
    """
    fallback: dict[str, Any] | None = None
    for d in _walk_dicts(item):
        if not isinstance(d, dict):
            continue
        name = _anchor_name(d)
        if not name:
            continue

        atype = d.get("type") or d.get("anchorType") or d.get("anchor_type")
        try:
            is_shop_type = int(atype) in SHOP_ANCHOR_TYPES
        except (TypeError, ValueError):
            is_shop_type = False
        anchor_shaped = any(k in d for k in _ANCHOR_SHAPE_KEYS)
        has_pid = any(d.get(k) for k in _PRODUCT_ID_KEYS)
        url = _anchor_url(d)
        shop_url = bool(url and _SHOP_URL_RE.search(url))

        if has_pid or (is_shop_type and anchor_shaped):
            return {"name": name, "url": url or None, "anchor_type": atype}
        if shop_url and fallback is None:
            # URL だけが根拠。より確実なものが後から見つかるかもしれないので保留
            fallback = {"name": name, "url": url, "anchor_type": atype}
    return fallback


def product_intent_detail(desc: str, hashtags: list[str],
                          has_anchor: bool) -> tuple[float, list[str]]:
    """商品紹介動画らしさ (0-1) と、そう判定した **根拠の語** を返す.

    根拠を一緒に返すのは UI で見せるため。
    「なぜこの動画が商品紹介だと判定されたか」が画面に出ないと、
    ただ伸びている動画を混ぜていないことを利用者が確かめられない。
    """
    hay = (desc + " " + " ".join(hashtags)).lower()
    hits = [w for w in PRODUCT_INTENT_WORDS if w in hay]
    hits += sorted(set(_TOKEN_RE.findall(hay)))
    if has_anchor:
        # 商品リンクが付いている = その商品の紹介動画であることが確定
        return 1.0, hits
    if not hits:
        return 0.0, []
    return min(1.0, 0.35 + 0.15 * len(hits)), hits


def product_intent_score(desc: str, hashtags: list[str],
                         has_anchor: bool) -> float:
    """この動画がどれだけ「商品紹介動画」らしいか (0-1).

    商品リンク付きは確定 (1.0)。それ以外はキャプションとタグの語で推定する。
    無関係な動画をトレンド集計に混ぜないためのフィルタ。
    """
    return product_intent_detail(desc, hashtags, has_anchor)[0]


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
    intent, intent_words = product_intent_detail(desc, hashtags, anchor is not None)
    # 商品リンクが無くても「何を紹介しているか」をキャプションから取り出す。
    # 日本では Shop リンク付きの動画が少なく、リンク必須にすると
    # 伸びている紹介動画の大半が「商品不明」として捨てられてしまう。
    candidates = extract_products(desc, hashtags, anchor)

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
            "product_candidates": [
                {"name": c.name, "confidence": c.confidence, "source": c.source}
                for c in candidates],
            "product_intent": round(intent, 2),
            # 判定の根拠。UI に出して「なぜ商品紹介動画と見なしたか」を示す
            "intent_words": intent_words[:6],
            "query": query,
            "create_time": create_time,
            "music": ((item.get("music") or {}).get("title")
                      if isinstance(item.get("music"), dict) else None),
        },
    )


def open_browser(pw: Any, config: Any) -> Any:
    """TikTok 用のブラウザを開く.

    **プロフィールを保存する形で開く。**
    こうすると一度ログインすればその状態が残り、次回以降の収集でも使える。
    TikTok は未ログインだと検索結果を返さないことがあり、
    かといって Cookie を DevTools から手で写すのは現実的でないため。

    戻り値は :class:`BrowserContext`。呼び出し側が ``close()`` する。
    """
    profile = Path(getattr(config, "browser_profile_dir", "data/browser-profile"))
    profile.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(profile),
        headless=bool(getattr(config, "headless", False)),
        locale="ja-JP", timezone_id="Asia/Tokyo",
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )


#: TikTok が「見せない」ときに画面に出る文言
_BLOCK_HINTS = (
    ("ログイン", "login"),
    ("不明なエラー", "error"),
    ("しばらくしてからもう一度", "error"),
    ("something went wrong", "error"),
    ("captcha", "captcha"),
    ("認証", "captcha"),
    ("アクセスできません", "blocked"),
    ("access denied", "blocked"),
)


def diagnose_block(page_hint: str) -> list[str]:
    """0 件だったときに、次に何をすればいいかを日本語で返す.

    URL の一覧を出しても利用者には次の一手が分からない。
    画面に出ていた文言から状況を見分けて、具体的な操作を出す。
    """
    hay = (page_hint or "").lower()
    kinds = {kind for word, kind in _BLOCK_HINTS if word.lower() in hay}
    if not kinds:
        return []

    out = ["★ TikTok が動画一覧を返していません。"]
    if "captcha" in kinds:
        out += ["  認証 (CAPTCHA) が出ています。",
                "  開いたブラウザ画面で認証を手動で通してから、もう一度実行してください。"]
    if "login" in kinds or "error" in kinds:
        out += ["  ログインを求められている / エラー画面が出ています。",
                "  未ログインだと検索結果を出さないことがあります。次を試してください:",
                "    1. 開いた TikTok の画面で自分のアカウントにログインする",
                "    2. ログイン状態を使い回すため .env に TIKTOK_SESSION_COOKIE を設定する",
                "       (Chrome で tiktok.com を開き F12 → Application → Cookies →",
                "        sessionid の値をコピー)",
                "    3. しばらく時間を空けてから再実行する (短時間に何度も叩くと弾かれます)"]
    if "blocked" in kinds:
        out += ["  アクセスが拒否されています。時間を空けて再実行してください。"]
    return out


@register("tiktok_video")
class TikTokVideoCollector(Collector):
    """tiktok.com を実ブラウザで開き、商品紹介動画を収集する."""

    provides = (EntityType.VIDEO,)
    requires = "playwright + chromium (pip install playwright && playwright install chromium)"

    #: 直近の collect() で「何本見て何本を商品紹介動画として採用したか」。
    #: Radar がこれを DB に保存し、アプリの「分析対象」パネルに出す。
    #: クラス属性は既定値。collect() が毎回新しい dict を代入する (共有しない)。
    stats: dict[str, Any] = {}

    #: ページを開いてから XHR を待つ時間 (ミリ秒)
    #: TikTok の検索結果は描画後に遅れて item 一覧を取りに行くため、
    #: 短いと取り逃す。実機では 4.5 秒では足りなかった。
    settle_ms = 9000

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

    def max_age_days(self) -> float:
        """何日前までの動画を対象にするか (0 で無制限).

        トレンドを見るのが目的なので、既定は 60 日。
        何ヶ月も前の動画は「今何が伸びているか」を歪めるだけになる。
        """
        try:
            return max(0.0, float(self.config.raw.get("max_video_age_days", 30)))
        except (TypeError, ValueError):
            return 30.0

    def build_targets(self) -> list[tuple[str, str]]:
        """見に行くページの一覧を作る (見る順に並べる).

        **ハッシュタグページを先に見る。**
        TikTok の検索は未ログインだと弾かれることがあり
        (「不明なエラーが発生しました」+ ログイン要求)、
        検索だけに頼ると 0 件になる。
        ハッシュタグページは同じ話題の動画が並ぶうえ、検索より通りやすい。

        検索は後ろに置き、ハッシュタグで十分集まったら実行時に打ち切る。
        """
        seen: set[str] = set()
        targets: list[tuple[str, str]] = []

        def add(label: str, url: str) -> None:
            if url not in seen:
                seen.add(url)
                targets.append((label, url))

        # 明示指定のハッシュタグ → 検索語をタグとしても見る → 検索
        for h in self.hashtags():
            add(f"#{h}", f"https://www.tiktok.com/tag/{quote(h)}")
        for q in self.queries():
            tag = q.replace(" ", "").replace("　", "")
            if tag:
                add(f"#{tag}", f"https://www.tiktok.com/tag/{quote(tag)}")
        for q in self.queries():
            add(q, f"https://www.tiktok.com/search/video?q={quote(q)}")
        return targets

    #: これだけ集まったら残りのページは見に行かない (無駄な負荷をかけない)
    def _enough(self) -> int:
        return max(60, int(self.config.limit_per_type) * 4)

    def _usable_count(self, captured: list[dict[str, Any]]) -> int:
        """集めたもののうち、実際に分析対象になりそうな本数.

        ハッシュタグページは人気順なので古い動画が大量に混ざる。
        生の件数で打ち切ると「245 件集めたが 130 件は 30 日より古くて
        使えない」ということが起きるので、投稿日時で先に数えておく。
        """
        max_age_h = self.max_age_days() * 24.0
        now = time.time()
        seen: set[str] = set()
        n = 0
        for it in captured:
            vid = it.get("id") or it.get("itemId") or it.get("aweme_id")
            if not vid or str(vid) in seen:
                continue
            seen.add(str(vid))
            ct = _num(it.get("createTime") or it.get("create_time"))
            if not max_age_h or not ct or (now - ct) / 3600.0 <= max_age_h:
                n += 1
        return n

    def collect(self, region: str) -> list[Snapshot]:
        from playwright.sync_api import sync_playwright

        targets = self.build_targets()
        if not targets:
            return []

        captured: list[dict[str, Any]] = []
        captured_at = time.time()
        out: list[Snapshot] = []
        # 0 件だったときに原因を示せるよう、見かけた API 通信を控えておく
        other_api: dict[str, int] = {}
        page_hint = ""

        with sync_playwright() as pw:
            ctx = open_browser(pw, self.config)
            if self.config.tiktok_session_cookie:
                ctx.add_cookies([{
                    "name": "sessionid", "value": self.config.tiktok_session_cookie,
                    "domain": ".tiktok.com", "path": "/",
                }])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            current: dict[str, str] = {"q": ""}

            def on_response(resp: Any) -> None:
                url = resp.url
                if not is_item_list_url(url):
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

            enough = self._enough()
            for label, url in targets:
                usable = self._usable_count(captured)
                if usable >= enough:
                    log.info("十分に集まったので残りのページは見ません "
                             "(使えるもの %d 件 / 見つけた %d 件)",
                             usable, len(captured))
                    break
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

        min_intent = float(self.config.raw.get("min_product_intent", 0.35))
        max_age_h = self.max_age_days() * 24.0
        drop_food = bool(self.config.raw.get("exclude_food", True))
        skipped = skipped_old = skipped_food = 0
        with_link = 0
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
            # トレンドを見るので、何ヶ月も前の動画は対象にしない。
            # 古い動画は再生数だけ積み上がっていて「今の勢い」を歪める。
            age = snap.metrics.get(M.AGE_HOURS)
            if max_age_h and age is not None and age > max_age_h:
                skipped_old += 1
                continue
            if drop_food and is_food(snap.name, " ".join(snap.extra.get("hashtags") or []),
                                     (snap.extra.get("product") or {}).get("name")):
                skipped_food += 1
                continue
            if snap.extra.get("product"):
                with_link += 1
            snap.captured_at = captured_at
            out.append(snap)

        # 同じ動画が複数のタグ/検索に出てくるので、ここで重複を除く
        out = dedupe(out)
        with_link = sum(1 for s in out if (s.extra or {}).get("product"))
        named = sum(1 for s in out if (s.extra or {}).get("product_candidates"))

        log.info("見つけた動画 %d 件 → 分析対象 %d 件", len(captured), len(out))
        if skipped:
            log.info("  商品紹介ではない          %d 件を除外 (紹介度 %.2f 未満)",
                     skipped, min_intent)
        if skipped_old:
            log.info("  直近 %.0f 日より古い        %d 件を除外",
                     self.max_age_days(), skipped_old)
        if skipped_food:
            log.info("  食べ物系                  %d 件を除外", skipped_food)
        if out:
            log.info("  商品名が分かったもの      %d 件 "
                     "(うち商品リンク付き %d 件)", named, with_link)

        # 「何を見て、何を落としたか」を UI に出せるように残す。
        # 商品紹介動画だけを見ていることを利用者が確認できないと、
        # ただ伸びている動画を混ぜていないか判断できない。
        self.stats = {
            "queries": [label for label, _ in targets],
            "min_product_intent": min_intent,
            "max_age_days": self.max_age_days(),
            "exclude_food": drop_food,
            "seen": len(captured),
            "kept": len(out),
            "skipped_not_product": skipped,
            "skipped_old": skipped_old,
            "skipped_food": skipped_food,
            "with_shop_link": with_link,
        }

        if not captured:
            # ここが 0 だと、フィルタ以前に一覧そのものを受け取れていない。
            # 推測させないために、実際に見えたものを出す。
            log.warning("動画一覧の通信を1件も受け取れませんでした。")
            if self.config.headless:
                # 実機で確認された最頻の原因。表示ありなら同じ条件で取得できた。
                log.warning("  ★ headless (ブラウザ非表示) で実行しています。")
                log.warning("     TikTok は非表示ブラウザからの検索結果を返さないことがあります。")
                log.warning("     config.yaml の headless を false にしてください。")
            for line in diagnose_block(page_hint):
                log.warning("  %s", line)
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

        return out

    def _scroll(self, page: Any) -> None:
        """スクロールして追加の item_list をロードさせる."""
        rounds = max(3, min(int(self.config.limit_per_type / 8), 8))
        for _ in range(rounds):
            try:
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1800)
            except Exception:
                break
