"""ファイル出力 (通知の動作確認・ログ用).

Webhook を用意しなくても通知パイプラインを検証できるようにするためのもの。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Sequence

from ..config import Config
from ..models import TrendSignal
from . import format_lines


def send(config: Config, signals: Sequence[TrendSignal], title: str) -> bool:
    out = Path(config.report_dir) / "alerts"
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    (out / f"alert_{stamp}.txt").write_text(
        f"{title} ({len(signals)}件)\n\n" + "\n\n".join(format_lines(signals, limit=50)),
        encoding="utf-8",
    )
    (out / f"alert_{stamp}.json").write_text(
        json.dumps([s.to_dict() for s in signals], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return True
