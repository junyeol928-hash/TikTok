"""ターミナル向けの表示."""

from __future__ import annotations

from ..analysis.digest import Digest
from ..models import EntityType, M, TrendStage

TYPE_LABEL = {
    EntityType.PRODUCT: "商品 (TikTok Shop)",
    EntityType.HASHTAG: "ハッシュタグ",
    EntityType.SONG: "楽曲 / サウンド",
    EntityType.VIDEO: "動画",
    EntityType.KEYWORD: "検索キーワード",
    EntityType.CREATOR: "クリエイター",
}

STAGE_COLOR = {
    TrendStage.EMERGING: "bold green",
    TrendStage.NEW: "bold cyan",
    TrendStage.RISING: "green",
    TrendStage.STABLE: "white",
    TrendStage.PEAKING: "yellow",
    TrendStage.DECLINING: "red",
}


def _num(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v/1e8:.1f}億"
    if v >= 1e4:
        return f"{v/1e4:.1f}万"
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.0f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.0%}"


def render(digest: Digest, top_n: int = 10, show_reasons: bool = True) -> None:
    """rich があればテーブル表示、無ければプレーンテキスト."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
    except ImportError:
        _render_plain(digest, top_n)
        return

    console = Console()
    import datetime as _dt
    ts = _dt.datetime.fromtimestamp(digest.generated_at).strftime("%Y-%m-%d %H:%M")
    console.print()
    console.rule(f"[bold]TikTok トレンドレーダー[/bold]  {digest.region}  {ts}")

    if digest.total_entities == 0:
        console.print("\n[yellow]データがありません。まず `ttradar collect` を実行してください。[/yellow]\n")
        return

    if digest.insufficient_history:
        console.print(
            f"[dim]※ {digest.insufficient_history} 件は履歴が不足しており伸び率を計算できません。"
            f"時間を空けて複数回 collect すると精度が上がります。[/dim]"
        )

    order = [EntityType.PRODUCT, EntityType.HASHTAG, EntityType.KEYWORD,
             EntityType.SONG, EntityType.VIDEO, EntityType.CREATOR]
    for etype in order:
        sigs = digest.top(etype, top_n)
        if not sigs:
            continue
        table = Table(
            title=f"\n{TYPE_LABEL.get(etype, etype.value)}",
            box=box.SIMPLE_HEAD, title_justify="left", header_style="bold",
            expand=True, pad_edge=False,
        )
        table.add_column("#", width=2, justify="right", no_wrap=True)
        table.add_column("点", width=3, justify="right", no_wrap=True)
        table.add_column("状態", width=9, no_wrap=True)
        # 名前と根拠は 1 セルに 2 行で入れる。
        # 根拠を別カラムにすると幅が足りずヘッダーごと潰れるため。
        table.add_column("名前 / 根拠", ratio=1, overflow="fold")
        table.add_column("現在", width=7, justify="right", no_wrap=True)
        table.add_column("伸び", width=7, justify="right", no_wrap=True)
        if etype == EntityType.PRODUCT:
            table.add_column("報酬", width=4, justify="right", no_wrap=True)
            table.add_column("競合", width=5, justify="right", no_wrap=True)

        for i, s in enumerate(sigs, 1):
            color = STAGE_COLOR.get(s.stage, "white")
            name_cell = f"[bold]{s.name}[/bold]"
            if show_reasons and s.reasons:
                name_cell += f"\n[dim]{' / '.join(s.reasons[:3])}[/dim]"
            row = [
                str(i),
                f"[{color}]{s.score:.0f}[/{color}]",
                f"[{color}]{s.stage.emoji}{s.stage.label_ja}[/{color}]",
                name_cell,
                _num(s.current_value),
                _pct(s.growth_rate),
            ]
            if etype == EntityType.PRODUCT:
                cr = s.metrics.get(M.COMMISSION_RATE)
                row.append(f"{cr:.0%}" if cr is not None else "—")
                row.append(_num(s.metrics.get(M.RELATED_VIDEOS)))
            table.add_row(*row)
        console.print(table)
    console.print()


def _render_plain(digest: Digest, top_n: int) -> None:
    print(f"\n=== TikTok トレンドレーダー [{digest.region}] ===")
    for etype, sigs in digest.by_type.items():
        if not sigs:
            continue
        print(f"\n-- {TYPE_LABEL.get(etype, etype.value)} --")
        for i, s in enumerate(sigs[:top_n], 1):
            print(f"{i:2}. [{s.score:5.1f}] {s.stage.label_ja:<5} {s.name[:32]:<34} "
                  f"現在 {_num(s.current_value):>8}  伸び {_pct(s.growth_rate):>7}")
            if s.reasons:
                print(f"      {' / '.join(s.reasons[:3])}")
    print()


def render_alerts(signals: list, threshold: float) -> None:
    """通知対象のサマリを出す."""
    try:
        from rich.console import Console
        from rich.panel import Panel
    except ImportError:
        for s in signals:
            print(f"[ALERT {s.score:.0f}] {s.name} — {' / '.join(s.reasons[:2])}")
        return
    console = Console()
    if not signals:
        console.print(f"[dim]スコア {threshold:.0f} 以上の新規シグナルはありません。[/dim]")
        return
    lines = []
    for s in signals[:20]:
        lines.append(
            f"[bold]{s.stage.emoji} {s.score:.0f}点[/bold]  {s.name}\n"
            f"   [dim]{' / '.join(s.reasons[:3])}[/dim]"
        )
    console.print(Panel("\n".join(lines),
                        title=f"🔔 要チェック ({len(signals)} 件)",
                        border_style="green"))
