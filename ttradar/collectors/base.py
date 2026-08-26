"""collector の基底とレジストリ.

契約
----
- collector は :class:`Snapshot` のリストだけを返す
- 1 つの collector が落ちても全体は止めない (:meth:`safe_collect` が吸収)
- 依存や API キーが無い collector は ``available()`` で False を返して黙って外れる

TikTok 系レスポンスは仕様が頻繁に変わるため、キー名をハードコードせず
:func:`pluck` / :func:`parse_count` で「ありそうなキー」を総当たりする方針を取る。
これが無いと TikTok 側の小変更で毎回壊れる。
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any, Iterable, Sequence

from ..config import Config
from ..models import EntityType, Snapshot
from ..util.http import BlockedError, HttpClient
from ..util.log import get

log = get(__name__)

_REGISTRY: dict[str, type["Collector"]] = {}


def register(name: str):
    """collector をレジストリに登録するデコレータ."""

    def deco(cls: type["Collector"]) -> type["Collector"]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def get_collector(name: str) -> type["Collector"] | None:
    return _REGISTRY.get(name)


def all_collectors() -> dict[str, type["Collector"]]:
    return dict(_REGISTRY)


class CollectorError(RuntimeError):
    pass


class Collector(ABC):
    """全 collector の基底."""

    name: str = "base"
    #: この collector が取得できるエンティティ種別
    provides: Sequence[EntityType] = ()
    #: 認証や外部バイナリを必要とするか (doctor の表示に使う)
    requires: str = ""

    def __init__(self, config: Config):
        self.config = config
        self._http: HttpClient | None = None

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(
                min_interval=self.config.request_interval,
                timeout=self.config.timeout,
                retries=self.config.retries,
            )
        return self._http

    def available(self) -> tuple[bool, str]:
        """利用可能かと、その理由を返す."""
        return True, "ok"

    @abstractmethod
    def collect(self, region: str) -> list[Snapshot]:
        """1 リージョン分を取得する."""

    def safe_collect(self, region: str) -> tuple[list[Snapshot], str | None]:
        """例外を握りつぶして (結果, エラー文字列) を返す."""
        ok, why = self.available()
        if not ok:
            return [], f"{self.name}: 利用不可 ({why})"
        try:
            t0 = time.time()
            snaps = self.collect(region)
            log.info("%s[%s]: %d 件取得 (%.1fs)",
                     self.name, region, len(snaps), time.time() - t0)
            return snaps, None
        except BlockedError as e:
            return [], f"{self.name}: ネットワーク制限で到達不可 — {e}"
        except Exception as e:  # noqa: BLE001 - 1 ソースの失敗で全体を止めない
            log.exception("%s[%s] が失敗しました", self.name, region)
            return [], f"{self.name}: {type(e).__name__}: {e}"

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


# ---------------------------------------------------------------- 正規化ヘルパ

_NUM_RE = re.compile(r"^\s*([\d,]*\.?\d+)\s*([KkMmBb万億千]?)\s*$")
_SUFFIX = {
    "": 1, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000,
    "b": 1_000_000_000, "B": 1_000_000_000,
    "千": 1_000, "万": 10_000, "億": 100_000_000,
}


def parse_count(value: Any) -> float | None:
    """TikTok が返す雑多な数値表現を float にする.

    ``"1.2M"`` / ``"12,345"`` / ``"3.4万"`` / ``1234`` / ``"1.5K"`` を受け付ける。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in {"-", "--", "N/A", "null"}:
        return None
    s = s.replace("+", "").replace(" ", "")
    # パーセント表記
    if s.endswith("%"):
        try:
            return float(s[:-1].replace(",", "")) / 100.0
        except ValueError:
            return None
    m = _NUM_RE.match(s)
    if not m:
        # 通貨記号などを剥がして再挑戦
        cleaned = re.sub(r"[^\d.,KkMmBb万億千]", "", s)
        m = _NUM_RE.match(cleaned)
        if not m:
            return None
    num, suffix = m.group(1), m.group(2)
    try:
        return float(num.replace(",", "")) * _SUFFIX.get(suffix, 1)
    except ValueError:
        return None


def pluck(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """複数の候補キーから最初に見つかった値を返す.

    ``pluck(d, "video_views", "views", "play_count")`` のように使う。
    ドット区切りでネストも辿れる: ``pluck(d, "stats.play_count")``
    """
    for key in keys:
        cur: Any = obj
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None and cur != "":
            return cur
    return default


def pluck_count(obj: dict[str, Any], *keys: str) -> float | None:
    """:func:`pluck` してから :func:`parse_count` する."""
    return parse_count(pluck(obj, *keys))


def find_list(payload: Any, *hints: str) -> list[dict[str, Any]]:
    """レスポンス JSON から「本体のリスト」を掘り出す.

    Creative Center は ``data.list`` だったり ``data.hashtag_list`` だったり
    ``data.materials`` だったりする。構造を仮定せず再帰的に探すことで、
    キー名が変わっても壊れないようにする。
    """
    if payload is None:
        return []

    # 1) ヒントで明示指定されたパスを優先
    if isinstance(payload, dict):
        for hint in hints:
            cur: Any = payload
            ok = True
            for part in hint.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list) and cur and isinstance(cur[0], dict):
                return cur

    # 2) 再帰的に「dict のリスト」で最大のものを拾う
    best: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 6:
            return
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and len(node) > len(best):
                best = node
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)

    walk(payload)
    return best


def dedupe(snapshots: Iterable[Snapshot]) -> list[Snapshot]:
    """同一 entity_key の重複を除く (メトリクスが多い方を残す)."""
    best: dict[str, Snapshot] = {}
    for s in snapshots:
        k = s.entity_key
        cur = best.get(k)
        if cur is None or len(s.metrics) > len(cur.metrics):
            best[k] = s
    return list(best.values())
