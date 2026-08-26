"""通知チャンネル.

「随時知りたい」を満たすための出口。
ただし全件を投げるとノイズになって見なくなるため、
- スコア閾値超え
- かつ直近 N 時間に同じものを通知していない
ものだけを送る (重複排除は :class:`ttradar.db.Database` が担当)。
"""

from __future__ import annotations

from typing import Callable, Sequence

from ..config import Config
from ..models import TrendSignal
from ..util.log import get

log = get(__name__)

Notifier = Callable[[Config, Sequence[TrendSignal], str], bool]


def get_notifier(channel: str) -> Notifier | None:
    if channel == "slack":
        from .slack import send as f
        return f
    if channel == "discord":
        from .discord import send as f
        return f
    if channel == "email":
        from .mail import send as f
        return f
    if channel == "file":
        from .filesink import send as f
        return f
    return None


def format_lines(signals: Sequence[TrendSignal], limit: int = 12) -> list[str]:
    """通知本文の共通フォーマット (プレーンテキスト)."""
    lines: list[str] = []
    for s in signals[:limit]:
        cur = f"{s.current_value:,.0f}" if s.current_value is not None else "—"
        gr = f"{s.growth_rate:+.0%}" if s.growth_rate is not None else "—"
        lines.append(
            f"{s.stage.emoji} [{s.score:.0f}点] {s.name}\n"
            f"    {s.entity_type.value} / 現在 {cur} / 伸び {gr}\n"
            f"    {' / '.join(s.reasons[:2])}"
            + (f"\n    {s.url}" if s.url else "")
        )
    if len(signals) > limit:
        lines.append(f"…ほか {len(signals) - limit} 件")
    return lines


def dispatch(config: Config, signals: Sequence[TrendSignal],
             title: str = "TikTok トレンド速報") -> dict[str, bool]:
    """設定済みの全チャンネルに送る. 戻り値は channel -> 成否."""
    results: dict[str, bool] = {}
    if not signals:
        return results
    for channel in config.enabled_notifiers():
        fn = get_notifier(channel)
        if fn is None:
            log.warning("未知の通知チャンネル: %s", channel)
            continue
        try:
            results[channel] = fn(config, signals, title)
        except Exception as e:  # noqa: BLE001
            log.warning("%s への通知に失敗: %s", channel, e)
            results[channel] = False
    return results
