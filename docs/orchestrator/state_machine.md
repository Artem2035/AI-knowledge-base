# Документация: `orchestrator/state_machine.py` — не-LLM конечный автомат задачи

> Reference-док. Обзор пакета — `_index.md`. Бюджет и исключения — `budget.md`. Порядок и содержимое шагов одного запроса — `../flows/llm_cycle.md`. Структуры данных — `../storage/models.md`.

**Назначение.** Чистый Python, который: нормализует запрос в `Task`; вызывает роли в фиксированной последовательности; прокидывает между этапами структурированные данные; контролирует бюджет LLM-вызовов и останавливается при его исчерпании (без fallback на платный tier); **сохраняет прогресс после каждого шага и батча** (`../staging/checkpoint.md`), чтобы задачу можно было продолжить через `resume <task_id>`; сам никогда не пишет в реальный Vault (это `staging/commit.py` после `approve`).

**Бюджет при resume.** `MAX_LLM_CALLS_PER_TASK` — лимит на одну **сессию**: `status.llm_calls_used` обнуляется в начале каждого `run()`, в том числе при resume. Накопительный расход по всем попыткам хранится в `TaskCheckpoint.total_llm_calls_used` (только для отчёта).

## 1. `KNOWLEDGE_MODE_FRONTMATTER_SOURCE: str = "model-knowledge"`

Значение `frontmatter.source` для заметок, написанных без внешних источников. Передаётся в `build_draft_note(mark_source=…)`. **MOC этого ключа не получает** (`build_moc` больше не принимает `mark_source`), поэтому diff не помечает MOC как «без внешних источников». Константа `roles/elaborator.py::MODEL_KNOWLEDGE_SOURCE_ID` относилась к удалённому `Evidence`.

## 2. `class OrchestratorStopped(Exception)`

Управляемая остановка: чекпоинт не найден, повреждён или другой версии при resume; либо `settings.research_mode == "web"` (заблокирован). Конструктор `__init__(message, task_id)`.

## 3. `class RunResult` (dataclass)

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Идентификатор задачи. |
| `changeset` | `StagingChangeset \| None` | `None`, если задача остановлена до staging. При ошибках валидации заполнен (changeset сохранён, `validation.ok=False`). |
| `status` | `TaskStatus` | Счётчик вызовов этой сессии. |
| `stopped` | `bool` | `True`: остановка по бюджету, лимиту или отказу утвердить план. Ошибки валидации остановкой не считаются. |
| `message` | `str` | Сообщение для CLI (при ошибках валидации содержит команду `resume`). |

## 4. `class Orchestrator`

### `__init__(self, settings: Settings)`
Проверяет `FREE_ONLY`, создаёт рабочие каталоги, открывает `VaultDB`, пробует создать эмбеддер (`try_create_embedder`), строит два `LLMBudget` и два клиента через `llm/factory.py`. Клиент extraction создаётся с `primary_client=self.llm`: при совпадающей модели он делит `TokenRateLimiter` с основным (`../llm/core.md §3.3`).

| Атрибут | Назначение |
|---|---|
| `self.db` | `VaultDB`. |
| `self.embedder` | `LocalEmbedder \| None`. |
| `self.budget` / `self.llm` | Основной бюджет и клиент: planner, vault_dedup, folder_assignment, annotator. |
| `self.extraction_budget` / `self.extraction_client` | Бюджет и клиент для Elaborator. |

**Исключения:** `RuntimeError` при `free_only=False` или пустом ключе провайдера.

### `close(self) -> None`
Закрывает `VaultDB` (вызывается в `finally` в `cli/main.py`).

### `sync_vault_index(self) -> dict`
Инкрементальная переиндексация Vault без LLM: `VaultIndexer.sync()` + `resolve_wikilink_targets()`. Возвращает статистику `scanned/updated/unchanged/removed`.

### `run(self, raw_query=None, *, resume_task_id=None, progress_cb=None, plan_confirm_cb=None, merge_confirm_cb=None) -> RunResult`

Выполняет workflow до staging включительно. Порядок (подробности — `../flows/llm_cycle.md`):

1. индексация Vault (всегда, без LLM);
2. **planning** → `checkpoint.plan`, `persist("planned")`;
3. **утверждение плана** `plan_confirm_cb` (один раз на задачу, флаг `plan_approved`); отказ → остановка, `persist("planned")`;
4. **elaboration** батчами, после каждого `persist("elaborating")`, в конце `elaboration_done`;
5. **vault analysis** (`vault_analysis_done`);
6. **annotation** батчами, `persist("annotating")`, в конце `annotation_done`;
7. **сборка** `build_draft_note` по каждой заметке плана (`mark_source="model-knowledge"`);
8. **слияние** `merge_confirm_cb(drafts)` и затем `fix_links_after_merge` — только если `settings.enable_draft_merging` **и** колбэк передан;
9. **автоисправление путей** `autofix_drafts(drafts, db.get_all_paths())` (`../validation/autofix.md`): суффикс « (2)» при конфликте пути с Vault или другой заметкой задачи; предупреждения `path_autofixed` передаются в `run_validation`;
10. **inline-ссылки** `apply_inline_links` для каждого черновика;
11. **MOC** `build_moc(...)`; если вернулся не `None`, ставится первым в список;
12. **relationships** `build_relationships(drafts)` — после слияния, автоисправления и MOC;
13. **validation** `run_validation(changeset, db, allow_delete, plan=plan, extra_issues=autofix_issues)`;
14. **staging** `save_changeset` (с `raw_query`). При `validation.ok` чекпоинт удаляется (`delete_checkpoint`). Иначе он сохраняется с меткой `validation_failed`, а `RunResult.message` содержит команду `resume`: шаги 7–14 не хранят состояния и не вызывают LLM, повторный запуск ничего не тратит.

Шаги 7–14 не имеют состояния и выполняются заново при каждом запуске (в том числе при resume); `DraftNote` в чекпоинте не хранятся. `vault_analysis_done` при resume не пересчитывается: решения create/update остаются прежними, а конфликты путей закрывает автоисправление.

| Параметр | Тип | Назначение |
|---|---|---|
| `raw_query` | `str \| None` | Обязателен без `resume_task_id`. |
| `resume_task_id` | `str \| None` | Продолжить задачу. |
| `progress_cb` | `Callable[[str], None] \| None` | Метка прогресса. |
| `plan_confirm_cb` | `Callable[[Plan], bool] \| None` | Без колбэка план утверждается автоматически. |
| `merge_confirm_cb` | `Callable[[list[DraftNote]], list[DraftNote]] \| None` | Работает только при `enable_draft_merging=True`. |

**Ловятся внутри:** `LLMFreeLimitReached`, `LLMTaskBudgetExceeded`: `persist(checkpoint.last_completed_stage)` и `RunResult(stopped=True)` с командой resume.

**Поднимаются наружу:** `OrchestratorStopped` (web-режим или нет чекпоинта), `ValueError` (нет ни `raw_query`, ни `resume_task_id`).

**Нюанс:** проверка `research_mode == "web"` выполняется после `_load_or_create_state`, поэтому для новой задачи пустой чекпоинт успевает сохраниться до `OrchestratorStopped`. На работу не влияет (виден в `resumable` как шаг `created`).

**Локальные функции:** `report(stage)` — прокси к `progress_cb`; `persist(stage_label)` — сохраняет чекпоинт (обновляет `last_completed_stage`, `status`, `total_llm_calls_used`). Вызывается после каждого батча и при `validation_failed` — это и есть механизм resume.

### `_load_or_create_state(self, *, raw_query, resume_task_id, report) -> tuple[TaskCheckpoint, Task, TaskStatus, int]`
Загружает чекпоинт (resume) или создаёт `Task`/`TaskStatus`/`TaskCheckpoint` и сразу сохраняет его. Возвращает `(checkpoint, task, status, base_total_calls)`; `status` всегда новый (`llm_calls_used=0`).

**Исключения:** `OrchestratorStopped` — чекпоинт не найден/повреждён/другой версии; `ValueError` — нет `raw_query`.

---

## Какие поля `Settings` читает модуль

| Поле | Где |
|---|---|
| `free_only`, пути (`workdir`, `staging_dir`, `checkpoint_dir`, `db_path`) | `__init__` |
| `embedding_model`, `use_local_embeddings` | `__init__` |
| `llm_provider`, `max_llm_calls_per_task`, `groq_*` soft-лимиты | `__init__` (через `llm/factory.py`) |
| `research_mode` | `run()` — блокирует `"web"`, выбирает `mark_source` |
| `max_subpoints_per_generation_batch` | `run()` → `elaborate_outline` |
| `default_notes_folder` | `run()` — папка темы для Vault Analyst и MOC |
| `dedup_high_threshold`, `dedup_low_threshold` | `run()` → `resolve_notes_against_vault` |
| `enable_draft_merging` | `run()` — шаг слияния |
| `allow_delete` | `run()` → `run_validation` |
| `language` | `_load_or_create_state` |
| `llm_provider` | подписи прогресса |

`draft_merge_mode` читает `cli/main.py`.

Документация по `orchestrator/state_machine.py` завершена. Далее — `../flows/llm_cycle.md`.