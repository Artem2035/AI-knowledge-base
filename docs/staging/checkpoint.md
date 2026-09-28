# Документация: `staging/checkpoint.py` — чекпоинты задач для resume

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Принципиально ОТДЕЛЬНЫЙ механизм персистентности от
`StagingChangeset` (`changeset.md`): тот создаётся только на ПОСЛЕДНЕМ
шаге, а `TaskCheckpoint` перезаписывается ПОСЛЕ КАЖДОГО завершённого шага
— это делает `resume` возможным, если задача остановилась на исчерпании
`MAX_LLM_CALLS_PER_TASK` задолго до staging (например, посреди
`elaborating`).

Инвариант: чекпоинт хранит УЖЕ ПРОВАЛИДИРОВАННЫЕ структурированные данные
(`Plan`, `Evidence[]`, ...) — те же Pydantic-модели
(`../storage/models.md`), поэтому сериализация тривиальна и не завязана
на провайдера.

**Гранулярность resume:**
- Planning / Vault-analysis — шаг целиком (флаги `*_done`).
- Elaborating — на уровне `subpoint_id` (`extracted_unit_ids`).
- Synthesis/Writer — на уровне ОТДЕЛЬНОЙ ЗАМЕТКИ (`written_note_indices` —
  индекс в `plan.notes`).

## 1. `CHECKPOINT_VERSION: int = 4`

Версия схемы чекпоинта. Бампается при несовместимых изменениях — старые
чекпоинты просто не загружаются (`load_checkpoint` вернёт `None`), а не
падают невнятной ошибкой Pydantic. v4 = переименование
`gemini_calls_*` → `llm_calls_*` в `TaskStatus`.

## 2. `class TaskCheckpoint(BaseModel)`

Снимок состояния задачи после последнего успешно завершённого шага.

| Поле | Тип | Назначение |
|---|---|---|
| `version` | `int` | Дефолт `CHECKPOINT_VERSION`. |
| `task_id`, `raw_query`, `language` | `str` | Обязательные. |
| `last_completed_stage` | `str` | Дефолт `"created"`. Только для отображения пользователю; логика resume опирается на `*_done` флаги. |
| `status` | `TaskStatus` | Снимок счётчиков на момент последнего `persist(...)`. |
| `total_llm_calls_used` | `int` | Накопительный расход по ВСЕМ попыткам (для отчёта). Отдельно от `status.llm_calls_used` (только текущая сессия) — лимит применяется к сессии, иначе задачу нельзя было бы докрутить после исчерпания. |
| `plan` | `Plan \| None` | Дефолт `None`. |
| `plan_approved` | `bool` | Дефолт `False`. Подтверждение плана (`../cli/plan_editor.md`), отдельно от `plan is not None`: при resume неутверждённого плана подтверждение запросится снова. |
| `extraction_done` | `bool` | Общий флаг elaborating. |
| `extracted_unit_ids` | `list[str]` | `subpoint_id` уже обработанных единиц. |
| `evidence` | `list[Evidence]` | Накапливается по батчам. |
| `vault_analysis_done` | `bool` | Мутирует `plan.notes` напрямую. |
| `existing_notes` | `list[ExistingNote]` | Архивное поле. |
| `synthesis_done` | `bool` | |
| `written_note_indices` | `list[int]` | Индексы уже написанных заметок в `plan.notes`. |
| `drafts` | `list[DraftNote]` | Накапливается по одной заметке. |
| `relationships` | `list[Relationship]` | |
| `updated_at` | `str` | UTC ISO-время. |

Также в модели есть поля для web-режима (`raw_candidates*`,
`selected_sources`, `fetched_sources`, `sources_*_done`) — не
используются в активном пути (см. `../CONTRIBUTING.md`).

## 3. `checkpoint_path(checkpoint_dir: Path, task_id: str) -> Path`

`Path(checkpoint_dir) / f"{task_id}.json"`. Не поднимает.

## 4. `save_checkpoint(checkpoint_dir: Path, checkpoint: TaskCheckpoint) -> None`

**Описание.** Атомарная запись: во временный `<task_id>.json.tmp`, затем
`replace` поверх основного — `kill -9` посреди записи не оставит битый
JSON. Важно, т.к. пишется ЧАСТО. Обновляет `checkpoint.updated_at`;
создаёт `checkpoint_dir`, если нужно.

**Исключения:** `OSError` — не перехватывается.

## 5. `load_checkpoint(checkpoint_dir: Path, task_id: str) -> TaskCheckpoint | None`

**Возвращаемое значение:** `None`, если файл не существует; либо (с
`logger.error`/`warning`) при повреждённом JSON, несовместимой версии,
ошибке валидации Pydantic. Иначе — валидный `TaskCheckpoint`. Вызывающий
код (`../orchestrator/state_machine.md`, `_load_or_create_state`)
интерпретирует `None` как "resume невозможен" → `OrchestratorStopped`.

**Исключения:** НЕ поднимает.

## 6. `delete_checkpoint(checkpoint_dir: Path, task_id: str) -> None`

Удаляет файл (`unlink(missing_ok=True)`). Вызывается в конце
`Orchestrator.run()`, когда задача доведена до staging.

## 7. `list_resumable_tasks(checkpoint_dir: Path) -> list[TaskCheckpoint]`

Все незавершённые чекпоинты (для команды `resumable`). Файлы
несовместимой версии и повреждённые пропускаются с `logger.warning`.
Отсортировано по имени файла, не по времени. Пустой список, если
директории нет.

Документация по `staging/checkpoint.py` завершена. Далее — `changeset.md`.
