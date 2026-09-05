from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from config.settings import get_settings
from orchestrator.state_machine import Orchestrator
from staging.changeset import list_pending_tasks, load_changeset, staging_task_dir
from staging.commit import commit_changeset
from staging.diff import render_diff_summary

app = typer.Typer(add_completion=False, help="Персональная AI-система управления знаниями для Obsidian")
console = Console()

logging.basicConfig(level=logging.WARNING)


@app.command()
def ask(query: str = typer.Argument(..., help="Запрос на естественном языке, напр. 'Изучи тему RAG'")):
    """Запустить полный workflow: анализ -> план -> исследование -> ... -> staging."""
    settings = get_settings()
    orch = Orchestrator(settings)

    console.print(Panel(f"Задача: {query}", title="obsidian-ai-kb", style="cyan"))

    def progress_cb(stage: str, _p={"task": None, "prog": None}):
        console.print(f"[dim]→[/dim] {stage}")

    try:
        with console.status("Выполняется…", spinner="dots"):
            result = orch.run(query, progress_cb=progress_cb)
    finally:
        orch.close()

    if result.stopped:
        console.print(
            Panel(
                result.message,
                title="⏸ Задача остановлена",
                style="yellow",
            )
        )
        console.print(
            f"Прогресс сохранён локально (потрачено вызовов Gemini: "
            f"{result.status.gemini_calls_used}/{settings.max_gemini_calls_per_task})."
        )
        raise typer.Exit(code=2)

    console.print(Panel(render_diff_summary(result.changeset), title="Предлагаемые изменения", style="green"))

    if result.changeset.validation and not result.changeset.validation.ok:
        console.print(
            "[red]Валидация нашла ошибки — approve заблокирован, пока они не исправлены.[/red]"
        )
        raise typer.Exit(code=1)

    console.print(
        f"\n[bold]Задача сохранена в staging:[/bold] {staging_task_dir(settings.staging_dir, result.task_id)}"
    )
    console.print(
        f"Чтобы применить изменения к реальному Vault: "
        f"[bold]python -m cli.main approve {result.task_id}[/bold]"
    )


@app.command()
def approve(task_id: str):
    """Применить staged-изменения к реальному Vault после ручной проверки."""
    settings = get_settings()
    changeset = load_changeset(settings.staging_dir, task_id)
    if changeset is None:
        console.print(f"[red]Задача {task_id} не найдена в staging.[/red]")
        raise typer.Exit(code=1)

    console.print(Panel(render_diff_summary(changeset), title="Изменения к применению", style="yellow"))

    if changeset.validation and not changeset.validation.ok:
        console.print("[red]Changeset не прошёл валидацию, commit заблокирован.[/red]")
        raise typer.Exit(code=1)

    confirmed = typer.confirm("Применить эти изменения к реальному Vault?", default=False)
    if not confirmed:
        console.print("Отменено.")
        raise typer.Exit(code=0)

    settings.ensure_dirs()
    db = None
    try:
        from tools.dedup import try_create_embedder
        from vault.db import VaultDB

        db = VaultDB(settings.db_path)
        embedder = try_create_embedder(settings.embedding_model, settings.use_local_embeddings)
        backup_dir = staging_task_dir(settings.staging_dir, task_id) / "backup_before_update"

        results = commit_changeset(
            changeset=changeset,
            vault_path=settings.vault_path,
            db=db,
            embedder=embedder,
            allow_delete=settings.allow_delete,
            git_enabled=settings.git_enabled,
            backup_dir=backup_dir,
        )
    finally:
        if db is not None:
            db.close()

    console.print(f"[green]Готово. Изменено файлов: {len(results)}.[/green]")
    for r in results:
        console.print(f"  {r.action}: {r.path}")


@app.command()
def pending():
    """Показать список задач, ожидающих approve в staging."""
    settings = get_settings()
    tasks = list_pending_tasks(settings.staging_dir)
    if not tasks:
        console.print("Нет задач, ожидающих подтверждения.")
        return
    for t in tasks:
        console.print(f"- {t}")


@app.command()
def index():
    """Просто пересканировать Vault и обновить локальный индекс (без Gemini)."""
    settings = get_settings()
    settings.ensure_dirs()
    from tools.dedup import try_create_embedder
    from vault.db import VaultDB
    from vault.index import VaultIndexer

    db = VaultDB(settings.db_path)
    embedder = try_create_embedder(settings.embedding_model, settings.use_local_embeddings)
    indexer = VaultIndexer(db=db, vault_path=settings.vault_path, embedder=embedder)
    stats = indexer.sync()
    indexer.resolve_wikilink_targets()
    db.close()
    console.print(stats)


if __name__ == "__main__":
    app()
