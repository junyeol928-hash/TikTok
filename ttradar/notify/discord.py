"""Discord Webhook 通知."""

from __future__ import annotations

from typing import Sequence

import requests

from ..config import Config
from ..models import TrendSignal
from ..util.log import get

log = get(__name__)

COLOR = {"emerging": 0x22C55E, "new": 0x38BDF8, "rising": 0x16A34A,
         "stable": 0x9CA3AF, "peaking": 0xF59E0B, "declining": 0xEF4444}


def send(config: Config, signals: Sequence[TrendSignal], title: str) -> bool:
    if not config.discord_webhook:
        return False
    embeds = []
    for s in signals[:10]:   # Discord の embed 上限は 10
        gr = f"{s.growth_rate:+.0%}" if s.growth_rate is not None else "—"
        cur = f"{s.current_value:,.0f}" if s.current_value is not None else "—"
        embeds.append({
            "title": f"{s.stage.emoji} {s.score:.0f}点 — {s.name}"[:250],
            "description": " / ".join(s.reasons[:3])[:1000],
            "url": s.url or None,
            "color": COLOR.get(s.stage.value, 0x9CA3AF),
            "fields": [
                {"name": "種別", "value": s.entity_type.value, "inline": True},
                {"name": "現在", "value": cur, "inline": True},
                {"name": "伸び率", "value": gr, "inline": True},
            ],
        })
    resp = requests.post(
        config.discord_webhook,
        json={"content": f"📡 **{title}** ({len(signals)}件)", "embeds": embeds},
        timeout=20,
    )
    if resp.status_code >= 300:
        log.warning("Discord への送信が失敗: %s %s", resp.status_code, resp.text[:200])
        return False
    return True
