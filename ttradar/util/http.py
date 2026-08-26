"""HTTP クライアント.

TikTok 系のエンドポイントは
  - レート制限が厳しい
  - たまに 200 で空の JSON を返す
  - User-Agent / Referer を見ている
ので、リトライ・スロットリング・UA 設定をここに集約する。

礼儀として既定でリクエスト間隔を空ける (DEFAULT_MIN_INTERVAL)。
相手のサーバーを叩き潰さないこと。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

import requests

from .log import get

log = get(__name__)

DEFAULT_MIN_INTERVAL = 1.2   # 同一ホストへの最小リクエスト間隔 (秒)
DEFAULT_TIMEOUT = 25.0
DEFAULT_RETRIES = 3

# Creative Center は素の python-requests UA を弾くことがあるため実ブラウザの UA を使う
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


class RateLimitError(RuntimeError):
    """429 / 明示的なレート制限."""


class BlockedError(RuntimeError):
    """ネットワークポリシー等でホストに到達できない (403/407 CONNECT 拒否含む)."""


class HttpClient:
    """スロットリング付きの薄い requests ラッパ."""

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        proxy: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._extra_headers = extra_headers or {}

    # -------------------------------------------------------------- 内部処理
    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_call.get(host, 0.0)
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.3))
            self._last_call[host] = time.time()

    def _headers(self, referer: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        h = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        }
        if referer:
            h["Referer"] = referer
            h["Origin"] = "https://" + referer.split("/")[2] if "//" in referer else referer
        h.update(self._extra_headers)
        if extra:
            h.update(extra)
        return h

    # ---------------------------------------------------------------- 公開 API
    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET して JSON を返す. 失敗時は例外."""
        host = url.split("/")[2] if "//" in url else url
        last_err: Exception | None = None

        for attempt in range(self.retries):
            self._throttle(host)
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=self._headers(referer, headers),
                    timeout=self.timeout,
                )
            except requests.exceptions.ProxyError as e:
                # 組織のegressポリシーによる拒否はリトライしても無駄
                raise BlockedError(
                    f"{host} へのアクセスがプロキシに拒否されました "
                    f"(ネットワークポリシー). 手元のPCで実行してください: {e}"
                ) from e
            except requests.exceptions.SSLError as e:
                raise BlockedError(f"{host} との TLS 検証に失敗: {e}") from e
            except requests.RequestException as e:
                last_err = e
                backoff = 2 ** attempt + random.uniform(0, 1)
                log.warning("通信失敗 (%s) %d/%d, %.1fs 後に再試行",
                            type(e).__name__, attempt + 1, self.retries, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code == 429:
                backoff = (2 ** attempt) * 5 + random.uniform(0, 3)
                log.warning("レート制限 (429). %.1fs 待機", backoff)
                time.sleep(backoff)
                last_err = RateLimitError(f"429 from {host}")
                continue
            if resp.status_code in (403, 407):
                raise BlockedError(
                    f"{host} が {resp.status_code} を返しました。"
                    "アクセス制限、またはネットワークポリシーによる拒否の可能性があります。"
                )
            if resp.status_code >= 500:
                backoff = 2 ** attempt + random.uniform(0, 1)
                log.warning("サーバーエラー %d. %.1fs 後に再試行", resp.status_code, backoff)
                time.sleep(backoff)
                last_err = RuntimeError(f"{resp.status_code} from {host}")
                continue
            resp.raise_for_status()

            try:
                return resp.json()
            except ValueError as e:
                snippet = resp.text[:200].replace("\n", " ")
                raise RuntimeError(
                    f"JSON として解釈できません (HTML が返っている可能性): {snippet}"
                ) from e

        raise last_err or RuntimeError(f"{url} の取得に失敗しました")

    def get_text(self, url: str, referer: str | None = None) -> str:
        host = url.split("/")[2] if "//" in url else url
        self._throttle(host)
        try:
            resp = self._session.get(url, headers=self._headers(referer, None),
                                     timeout=self.timeout)
        except requests.exceptions.ProxyError as e:
            raise BlockedError(f"{host} へのアクセスが拒否されました: {e}") from e
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._session.close()
