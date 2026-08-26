"""ロギング. rich があれば色付き、無ければ標準 logging にフォールバック."""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = logging.DEBUG if verbose or os.getenv("TTRADAR_DEBUG") else logging.INFO
    try:
        from rich.logging import RichHandler

        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="%H:%M:%S",
            handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)],
        )
    except Exception:  # pragma: no cover - rich 未インストール時のみ
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    # 依存ライブラリのログは黙らせる
    for noisy in ("urllib3", "requests", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
