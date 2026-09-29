# Документация: `cli/main.py` — точка входа Typer

> Reference-док. Обзор пакета — `_index.md`.

`app = typer.Typer(add_completion=False)`, `console = rich.Console()`, логирование `WARNING`.

## `_run_and_report(orch, *, raw_query, resume_task_id, settings) -> None`

Общая обвязка `ask`/`resume`. Определяет три callback'а для `orch.run(...)`:
- `progress_cb(stage)` — печатает `→ {stage}`;
- `confirm_with_paused_spinner(plan)` — останавливает spinner, вызывает `plan_editor.confirm_plan`, в `finally` перезапускает spinner (это `plan_confirm_cb`);
- `merge_confirm_with_paused_spinner(drafts)` — то же для `draft_merge_editor.confirm_merges(drafts, mode=settings.draft_merge_mode)` (это `merge_confirm_cb`).

После `run` (в `finally` — `orch.close()`):
- `result.stopped` → оранжевая панель, счётчик вызовов сессии (`llm_calls_used` / `max_llm_calls_per_task`), `Exit(2)`;
- иначе — diff (`../staging/diff.md`); если `validation` не `ok` → `Exit(1)`; иначе путь staging и подсказка `approve <task_id>`.

`OrchestratorStopped` здесь не перехватывается (см. `resume`).

## Команды

| Команда | Что делает | Исключения |
|---|---|---|
| `ask(query)` | `Settings` → `Orchestrator` → панель запроса → `_run_and_report(raw_query=query)`. | Не ловит. |
| `resume(task_id)` | То же с `resume_task_id`; `OrchestratorStopped` → красная панель, `Exit(1)`. | Ловит `OrchestratorStopped`. |
| `resumable()` | Без LLM. Список чекпоинтов (`../staging/checkpoint.md`): id, шаг, запрос (до 60 символов), время в `Europe/Moscow`, всего LLM-вызовов. | Не поднимает. |
| `approve(task_id)` | См. ниже. | `CommitError`, `FileExistsError`, `FileNotFoundError`, `PermissionError` всплывают. |
| `pending()` | Без LLM. Список задач в staging (`changeset.md`). | Не поднимает. |
| `index()` | Без LLM. `VaultIndexer.sync()` + `resolve_wikilink_targets()`, печать статистики. | `FileNotFoundError`, если `vault_path` нет. |

### `approve(task_id)` — единственная запись в Vault
1. `load_changeset`; `None` → ошибка, `Exit(1)`.
2. Печать diff (панель «Изменения к применению»).
3. `validation` не `ok` → блок, `Exit(1)`.
4. `typer.confirm(..., default=False)`; отказ → «Отменено.», `Exit(0)`.
5. `settings.ensure_dirs()`; `VaultDB`, `try_create_embedder`, `backup_dir = <staging_task_dir>/backup_before_update`.
6. `commit_changeset(...)` (`../staging/commit.md`) в `try/finally` с `db.close()`.
7. Печать числа файлов и `{action}: {path}` по каждому `WriteResult`.

Документация по `cli/main.py` завершена. Далее — `plan_editor.md`.
