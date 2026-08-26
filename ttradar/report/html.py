"""HTML ダッシュボードの生成."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analysis.digest import Digest
from ..models import EntityType, M, TrendStage
from .console import TYPE_LABEL, _num, _pct

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _price(v: float | None) -> str:
    return "—" if v is None else f"¥{v:,.0f}"


def build_html(digest: Digest, top_n: int = 15, sources: str = "") -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("digest.html.j2")

    order = [EntityType.PRODUCT, EntityType.HASHTAG, EntityType.KEYWORD,
             EntityType.SONG, EntityType.VIDEO, EntityType.CREATOR]
    blocks = []
    for etype in order:
        sigs = digest.top(etype, top_n)
        if not sigs:
            continue
        rows = []
        for s in sigs:
            rows.append({
                "score": s.score,
                "stage": s.stage.value,
                "stage_label": s.stage.label_ja,
                "stage_emoji": s.stage.emoji,
                "name": s.name,
                "url": s.url,
                "category": s.category,
                "why": " / ".join(s.reasons[:3]),
                "current": _num(s.current_value),
                "growth": _pct(s.growth_rate),
                "growth_raw": s.growth_rate,
                "commission": (f"{s.metrics[M.COMMISSION_RATE]:.0%}"
                               if M.COMMISSION_RATE in s.metrics else "—"),
                "competition": _num(s.metrics.get(M.RELATED_VIDEOS)),
                "price": _price(s.metrics.get(M.PRICE)),
            })
        blocks.append({
            "label": TYPE_LABEL.get(etype, etype.value),
            "rows": rows,
            "is_product": etype == EntityType.PRODUCT,
        })

    all_sigs = digest.all_signals()
    return tpl.render(
        date_str=dt.datetime.fromtimestamp(digest.generated_at).strftime("%Y-%m-%d %H:%M"),
        region=digest.region,
        window_hours=next((s.window_hours for s in all_sigs if s.window_hours), 24),
        total_entities=digest.total_entities,
        insufficient_history=digest.insufficient_history,
        n_emerging=sum(1 for s in all_sigs if s.stage == TrendStage.EMERGING),
        n_rising=sum(1 for s in all_sigs if s.stage == TrendStage.RISING),
        n_new=sum(1 for s in all_sigs if s.stage == TrendStage.NEW),
        n_alerts=sum(1 for s in all_sigs if s.score >= 70),
        blocks=blocks,
        sources=sources or "—",
    )


def write_report(digest: Digest, out_dir: str, top_n: int = 15,
                 sources: str = "") -> Path:
    """HTML を書き出し、パスを返す. latest.html も同時に更新する."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(digest.generated_at).strftime("%Y%m%d_%H%M")
    html = build_html(digest, top_n=top_n, sources=sources)
    path = d / f"digest_{stamp}.html"
    path.write_text(html, encoding="utf-8")
    # 常に最新を指すファイル (ブックマークしておける)
    (d / "latest.html").write_text(html, encoding="utf-8")
    return path
