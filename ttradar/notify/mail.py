"""SMTP メール通知."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Sequence

from ..config import Config
from ..models import TrendSignal
from ..util.log import get
from . import format_lines

log = get(__name__)


def send(config: Config, signals: Sequence[TrendSignal], title: str) -> bool:
    if not (config.smtp_host and config.email_to):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[ttradar] {title} ({len(signals)}件)"
    msg["From"] = config.smtp_user or "ttradar@localhost"
    msg["To"] = config.email_to
    body = "\n\n".join(format_lines(signals, limit=20))
    msg.set_content(f"{title}\n\n{body}\n")

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as srv:
        srv.starttls()
        if config.smtp_user and config.smtp_password:
            srv.login(config.smtp_user, config.smtp_password)
        srv.send_message(msg)
    return True
