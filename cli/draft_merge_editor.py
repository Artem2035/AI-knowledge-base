"""
Экран объединения уже написанных заметок — merge_confirm_cb в
Orchestrator.run(), вызывается ПОСЛЕ Writer+Critic, ДО validation/staging.
Активен только при settings.enable_draft_merging=True.
"""
from __future__ import annotations

import typer
from rich.console import Console

from staging.draft_merge import apply_merges
from storage.models import DraftNote

console = Console()


def confirm_merges(drafts: list[DraftNote], mode: str = "select") -> list[DraftNote]:
    create_count = sum(1 for d in drafts if d.action.value == "create")

    if mode == "all":
        from staging.draft_merge import merge_all_drafts

        if create_count < 2:
            console.print("[dim]Меньше двух новых заметок — объединять нечего.[/dim]")
            return drafts

        console.print(
            f"\n[bold]Объединение заметок[/bold] (режим 'all'): "
            f"все {create_count} новых заметок будут объединены в одну."
        )
        title = typer.prompt("Заголовок объединённой заметки (Enter — составить автоматически)", default="").strip()
        return merge_all_drafts(drafts, merged_title=title)

    # mode == "select" — прежнее поведение без изменений
    console.print("\n[bold]Объединение заметок[/bold] (необязательно — можно пропустить)")
    for i, d in enumerate(drafts, start=1):
        marker = "" if d.action.value == "create" else " (update — не участвует)"
        console.print(f"  {i}. {d.title}{marker}")

    groups: list[tuple[list[int], str]] = []
    used: set[int] = set()
    while typer.confirm("Объединить несколько заметок в одну?", default=False):
        raw = typer.prompt("Номера через запятую (например: 2,4,5)")
        try:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
        except ValueError:
            console.print("[red]Не удалось разобрать номера.[/red]")
            continue
        if len(set(indices)) < 2 or any(i < 0 or i >= len(drafts) for i in indices):
            console.print("[red]Нужно минимум 2 корректных номера.[/red]")
            continue
        if used & set(indices):
            console.print("[red]Заметка уже участвует в другом объединении.[/red]")
            continue
        if any(drafts[i].action.value != "create" for i in indices):
            console.print("[red]Объединять можно только новые заметки (action=create).[/red]")
            continue
        default_title = " + ".join(drafts[i].title for i in indices)
        title = typer.prompt("Заголовок объединённой заметки", default=default_title).strip()
        groups.append((indices, title))
        used.update(indices)

    return apply_merges(drafts, groups) if groups else drafts