"""Slack Incoming Webhook 通知."""

from __future__ import annotations

import json
from typing import Sequence

import requests

from ..config import Config
from ..models import TrendSignal
from ..util.log import get
from . import format_lines

log = get(__name__)


def send(config: Config, signals: Sequence[TrendSignal], title: str) -> bool:
    if not config.slack_webhook:
        return False
    blocks: list[dict] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"📡 {title} ({len(signals)}件)"},
    }]
    for s in signals[:10]:
        gr = f"{s.growth_rate:+.0%}" if s.growth_rate is not None else "—"
        cur = f"{s.current_value:,.0f}" if s.current_value is not None else "—"
        text = (f"*{s.stage.emoji} {s.score:.0f}点 — {s.name}*\n"
                f"{s.entity_type.value} / 現在 {cur} / 伸び {gr}\n"
                f"_{' / '.join(s.reasons[:2])}_")
        block: dict = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        if s.url:
            block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "開く"},
                "url": s.url,
            }
        blocks.append(block)
    if len(signals) > 10:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"…ほか {len(signals)-10} 件"}]})

    resp = requests.post(
        config.slack_webhook,
        data=json.dumps({"text": f"{title} ({len(signals)}件)", "blocks": blocks}),
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    if resp.status_code >= 300:
        log.warning("Slack への送信が失敗: %s %s", resp.status_code, resp.text[:200])
        return False
    return True
