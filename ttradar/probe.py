"""TikTok のページを開いて、実際に何が返ってきているかを調べる診断コマンド.

なぜ必要か
----------
tiktok_video collector が 0 件しか取れないとき、原因は次のどれか分からない:

  1. ログイン壁や年齢確認で、そもそも動画一覧が読み込まれていない
  2. TikTok が別の URL で一覧を返しており、傍受対象に入っていない
  3. 自動操作と判定されてブロックされている
  4. レスポンスは来ているが、こちらの解析が形に合っていない

推測で直しても当たらないので、**実際に飛んでいる通信を全部記録して見る**。
その結果をもとに傍受対象と解析を修正する。

使い方
------
    ttradar probe                      # 既定のクエリで調べる
    ttradar probe -q 購入品紹介 --visible   # ブラウザを表示して確認
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .collectors.tiktok_video import find_video_items
from .config import Config
from .util.log import get

log = get(__name__)

#: 記録から除外する明らかに無関係なもの (画像・動画本体・計測など)
IGNORE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".m4s",
              ".css", ".woff", ".woff2", ".ttf", ".svg", ".ico")
IGNORE_HOST_PARTS = ("google-analytics", "doubleclick", "facebook",
                     "byteoversea", "sentry", "tiktokcdn-", "ttwstatic")


def _interesting(url: str) -> bool:
    low = url.lower()
    if any(low.split("?")[0].endswith(e) for e in IGNORE_EXT):
        return False
    if any(p in low for p in IGNORE_HOST_PARTS):
        return False
    return True


def run_probe(cfg: Config, query: str, visible: bool, seconds: float,
              out_dir: str) -> int:
    """1 つのページを開き、飛んでいる通信を記録する."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright が入っていません。setup を実行してください。")
        return 1

    url = f"https://www.tiktok.com/search/video?q={quote(query)}"
    print(f"\n調査対象: {url}")
    print(f"ブラウザ: {'表示あり' if visible else 'headless'}   待機: {seconds:.0f} 秒\n")

    seen: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    page_title = ""
    page_text_head = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not visible,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            viewport={"width": 1400, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
        )
        if cfg.tiktok_session_cookie:
            ctx.add_cookies([{"name": "sessionid",
                              "value": cfg.tiktok_session_cookie,
                              "domain": ".tiktok.com", "path": "/"}])
            print("sessionid クッキーを使用します\n")

        page = ctx.new_page()

        def on_response(resp: Any) -> None:
            u = resp.url
            if not _interesting(u):
                return
            key = u.split("?")[0]
            rec = seen.setdefault(key, {
                "url": key, "count": 0, "status": resp.status,
                "ctype": "", "items": 0, "json": False,
            })
            rec["count"] += 1
            rec["status"] = resp.status
            ctype = (resp.headers or {}).get("content-type", "")
            rec["ctype"] = ctype.split(";")[0]
            if "json" not in ctype:
                return
            rec["json"] = True
            try:
                payload = resp.json()
            except Exception:
                return
            items = find_video_items(payload)
            if items:
                rec["items"] = max(rec["items"], len(items))
                if len(samples) < 3:
                    # 解析修正のため、1 件だけ構造を保存する (中身は切り詰める)
                    samples.append({
                        "url": key,
                        "item_keys": sorted(items[0].keys())[:40],
                        "sample": _trim(items[0]),
                    })

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"ページを開けませんでした: {e}")

        # スクロールして追加読み込みを誘発しつつ待つ
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                page.mouse.wheel(0, 2500)
            except Exception:
                pass
            page.wait_for_timeout(1500)

        try:
            page_title = page.title()
            body = page.inner_text("body")[:600]
            page_text_head = " ".join(body.split())
        except Exception:
            pass

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        shot = out / "probe_screenshot.png"
        try:
            page.screenshot(path=str(shot), full_page=False)
        except Exception:
            shot = None

        ctx.close()
        browser.close()

    # ------------------------------------------------------------- 結果表示
    print("=" * 62)
    print(f"ページタイトル: {page_title}")
    if page_text_head:
        print(f"画面の文言(先頭): {page_text_head[:200]}")
    print("=" * 62)

    api_like = {k: v for k, v in seen.items() if "/api/" in k or v["json"]}
    withitems = {k: v for k, v in api_like.items() if v["items"] > 0}

    print(f"\n通信 {len(seen)} 種類 / うち API・JSON {len(api_like)} 種類 "
          f"/ 動画らしきデータを含む {len(withitems)} 種類\n")

    if withitems:
        print("★ 動画データが見つかった通信:")
        for v in sorted(withitems.values(), key=lambda x: -x["items"]):
            print(f"   {v['items']:>4} 件  {v['status']}  {v['url']}")
    else:
        print("動画データを含む通信はありませんでした。")

    if api_like:
        print("\nそのほかの API 通信:")
        for v in sorted(api_like.values(), key=lambda x: x["url"]):
            if v["items"] > 0:
                continue
            print(f"   {v['status']}  {v['ctype'] or '-':<24} {v['url']}")

    report = {
        "query": query, "url": url, "title": page_title,
        "page_text_head": page_text_head,
        "responses": sorted(seen.values(), key=lambda x: x["url"]),
        "samples": samples,
        "generated_at": time.time(),
    }
    path = Path(out_dir) / "probe_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")

    print(f"\n詳細を書き出しました: {path}")
    if shot:
        print(f"画面の画像:           {shot}")
    print("\nこの 2 つのファイルを共有してもらえれば、"
          "傍受対象と解析を実データに合わせられます。")
    return 0


def _trim(obj: Any, depth: int = 0) -> Any:
    """保存用に大きな値を切り詰める (構造だけ分かればよい)."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [_trim(v, depth + 1) for v in obj[:3]]
    if isinstance(obj, str):
        return obj[:120]
    return obj
