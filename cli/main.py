from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from config.settings import get_settings

from orchestrator.state_machine import Orchestrator, OrchestratorStopped
from staging.changeset import list_pending_tasks, load_changeset, staging_task_dir
from staging.checkpoint import list_resumable_tasks
from staging.commit import commit_changeset
from staging.diff import render_diff_summary

app = typer.Typer(add_completion=False, help="Персональная AI-система управления знаниями для Obsidian")
console = Console()

logging.basicConfig(level=logging.WARNING)


def _run_and_report(orch: Orchestrator, *, raw_query: str | None, resume_task_id: str | None, settings) -> None:
    """Общая логика запуска (новая задача или resume) + единый вывод
    результата в консоль. Вынесена из ask()/resume(), чтобы поведение не
    расходилось между двумя командами."""

    def progress_cb(stage: str):
        console.print(f"[dim]→[/dim] {stage}")

    try:
        with console.status("Выполняется…", spinner="dots"):
            result = orch.run(
                raw_query=raw_query, resume_task_id=resume_task_id, progress_cb=progress_cb
            )
    finally:
        orch.close()

    if result.stopped:
        console.print(Panel(result.message, title="⏸ Задача остановлена", style="yellow"))
        console.print(
            f"Потрачено вызовов Gemini за эту сессию: "
            f"{result.status.gemini_calls_used}/{settings.max_gemini_calls_per_task}."
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
def ask(query: str = typer.Argument(..., help="Запрос на естественном языке, напр. 'Изучи тему RAG'")):
    """Запустить полный workflow: анализ -> план -> исследование -> ... -> staging."""
    settings = get_settings()
    orch = Orchestrator(settings)

    console.print(Panel(f"Задача: {query}", title="obsidian-ai-kb", style="cyan"))
    _run_and_report(orch, raw_query=query, resume_task_id=None, settings=settings)


@app.command()
def resume(task_id: str = typer.Argument(..., help="task_id остановленной задачи (см. 'resumable')")):
    """Продолжить ранее остановленную (по лимиту Gemini) задачу с последнего
    сохранённого шага — без повторного прохождения уже сделанной работы."""
    settings = get_settings()
    orch = Orchestrator(settings)

    console.print(Panel(f"Продолжение задачи: {task_id}", title="obsidian-ai-kb", style="cyan"))

    try:
        _run_and_report(orch, raw_query=None, resume_task_id=task_id, settings=settings)
    except OrchestratorStopped as exc:
        console.print(Panel(str(exc), title="Не удалось продолжить", style="red"))
        raise typer.Exit(code=1)


@app.command()
def resumable():
    """Показать задачи, остановленные по лимиту Gemini и доступные для resume."""
    settings = get_settings()
    checkpoints = list_resumable_tasks(settings.checkpoint_dir)
    if not checkpoints:
        console.print("Нет задач, ожидающих продолжения.")
        return
    for cp in checkpoints:
        preview = cp.raw_query if len(cp.raw_query) <= 60 else cp.raw_query[:57] + "…"
        console.print(
            f"- [bold]{cp.task_id}[/bold]  шаг: {cp.last_completed_stage}  "
            f"«{preview}»  (всего потрачено Gemini-вызовов: {cp.total_gemini_calls_used})"
        )
    console.print(
        "\nПродолжить: [bold]python -m cli.main resume <task_id>[/bold]"
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