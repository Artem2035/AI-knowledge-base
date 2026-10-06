# Документация: `staging/checkpoint.py` — чекпоинты задач для resume

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Механизм персистентности, отдельный от `StagingChangeset` (`changeset.md`): changeset создаётся только на последнем шаге, а `TaskCheckpoint` перезаписывается **после каждого завершённого шага и каждого батча**. Это делает `resume` возможным, если задача остановилась по лимиту (`MAX_LLM_CALLS_PER_TASK`, 429) задолго до staging.

**Инвариант.** Чекпоинт хранит уже провалидированные структурированные данные (`Plan`, `SectionDraft[]`, `NoteAnnotation[]`), те же Pydantic-модели (`../storage/models.md`), поэтому сериализация тривиальна и не зависит от провайдера. **`DraftNote` в чекпоинте не хранятся**: сборка заметки из секций и аннотаций (`../tools/note_assembly.md::build_draft_note`) детерминирована и дёшева, поэтому выполняется заново при каждом запуске.

**Гранулярность resume:**
- Planning, Vault analysis — шаг целиком (флаги `plan_approved`, `vault_analysis_done`).
- Elaboration — отдельный подпункт: `elaborated_subpoint_ids` + `sections`.
- Annotation — отдельная заметка: `annotated_note_ids` + `annotations`.
- Сборка, слияние, MOC, relationships, validation, staging — без состояния, всегда заново.

## 1. `CHECKPOINT_VERSION: int = 5`

Бампается при несовместимых изменениях. Чекпоинты другой версии **не загружаются** (`load_checkpoint` → `None`, `list_resumable_tasks` их пропускает), задачу нужно запустить заново командой `ask`.
- v4: `TaskStatus.gemini_calls_*` → `llm_calls_*`.
- **v5:** цепочка Elaborator → Evidence → Writer → Critic заменена на Elaborator (markdown) → Annotator → сборка кодом. Убраны `evidence`, `existing_notes`, `synthesis_done`, `written_note_indices`, `drafts`, `relationships`; `extraction_*` переименованы в `elaboration_*`; добавлены `sections`, `annotation_done`, `annotated_note_ids`, `annotations`.

## 2. `class TaskCheckpoint(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `version` | `int` | Дефолт `CHECKPOINT_VERSION`. |
| `task_id`, `raw_query`, `language` | `str` | Обязательные. |
| `last_completed_stage` | `str` | Дефолт `"created"`. Только для отображения; логика resume опирается на флаги. |
| `status` | `TaskStatus` | Снимок счётчиков на момент последнего `persist(...)`. |
| `total_llm_calls_used` | `int` | Накопительный расход по всем попыткам (для отчёта); лимит применяется к сессии. |
| `plan` | `Plan \| None` | Мутируется Vault Analyst (`action`/`existing_path`/`folder`), поэтому после анализа Vault сохраняется заново. |
| `plan_approved` | `bool` | Подтверждение плана пользователем, отдельно от `plan is not None`: неутверждённый план при resume показывается снова. |
| `elaboration_done` | `bool` | Все разделы написаны. |
| `elaborated_subpoint_ids` | `list[str]` | `subpoint_id` обработанных подпунктов, **включая placeholder'ы**. |
| `sections` | `list[SectionDraft]` | Готовый markdown разделов, копится по батчам. |
| `vault_analysis_done` | `bool` | Результат живёт в `plan.notes`, отдельного списка нет. |
| `annotation_done` | `bool` | Все create-заметки аннотированы. |
| `annotated_note_ids` | `list[str]` | `note_id` аннотированных заметок (включая получившие пустую аннотацию). |
| `annotations` | `list[NoteAnnotation]` | Копится по батчам. |
| `updated_at` | `str` | UTC ISO, обновляется при сохранении. |

Поля web-режима (`raw_candidates_done`, `raw_candidates`, `sources_selected_done`, `selected_sources`, `sources_fetched_done`, `fetched_sources`) оставлены без изменений: `RESEARCH_MODE=web` заблокирован в `Orchestrator.run()`, в активном пути они не используются.

## 3. `checkpoint_path(checkpoint_dir: Path, task_id: str) -> Path`
`checkpoint_dir / f"{task_id}.json"`. Не поднимает.

## 4. `save_checkpoint(checkpoint_dir: Path, checkpoint: TaskCheckpoint) -> None`
Атомарная запись: во временный `<task_id>.json.tmp`, затем `replace` поверх основного, так что `kill -9` посреди записи не оставит битый JSON. Важно, потому что пишется часто. Обновляет `checkpoint.updated_at`, создаёт каталог при необходимости. **Исключения:** `OSError` не перехватывается.

## 5. `load_checkpoint(checkpoint_dir: Path, task_id: str) -> TaskCheckpoint | None`
`None`, если файла нет; либо с `logger.error`/`warning`, если JSON повреждён, версия отличается от `CHECKPOINT_VERSION` или не прошла валидация Pydantic. Вызывающий код (`Orchestrator._load_or_create_state`) трактует `None` как «resume невозможен» → `OrchestratorStopped`. Сама функция исключений не поднимает.

## 6. `delete_checkpoint(checkpoint_dir: Path, task_id: str) -> None`
`unlink(missing_ok=True)`. Вызывается в конце `Orchestrator.run()`, когда задача доведена до staging.

## 7. `list_resumable_tasks(checkpoint_dir: Path) -> list[TaskCheckpoint]`
Все незавершённые чекпоинты **текущей версии** (для команды `resumable`). Повреждённые файлы пропускаются с `logger.warning`, файлы других версий пропускаются молча. Сортировка по имени файла. Пустой список, если каталога нет.

Документация по `staging/checkpoint.py` завершена. Далее — `changeset.md`.