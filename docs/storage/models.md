# Документация: `storage/models.py`

> Reference-док. Обзор пакета — `_index.md`. Единственный модуль со
> структурированными объектами, которыми обмениваются этапы workflow.

**Назначение (из докстринга модуля).** Правило проекта: между ролями
никогда не передаётся длинный "сырой" текст — только эти типизированные
Pydantic-модели. Это даёт: (1) валидацию на границах между ролями, (2)
возможность сериализовать в JSON и персистить прогресс на диск
(`../staging/checkpoint.md` — resume после исчерпания лимита LLM-провайдера),
(3) предсказуемый contract для structured-output вызовов LLM
(`../llm/schemas.md` — отдельные "выходные" Pydantic-модели именно под ответы
LLM, конвертируемые в объекты отсюда кодом ролей — LLM никогда не заполняет
эти модели напрямую).

---

## 0. Служебные функции модуля

### `_now() -> str`

Текущее время в UTC, ISO 8601 (`datetime.now(timezone.utc).isoformat()`).
`default_factory` для полей `created_at`/`timestamp`.

### `_new_id() -> str`

Генерирует короткий уникальный идентификатор: первые 12 hex-символов UUID4
(`uuid.uuid4().hex[:12]`). `default_factory` для всех `*_id` полей
(`task_id`, `note_id`, `subpoint_id`, `source_id`, `evidence_id`,
`draft_id`). Ключевой архитектурный принцип проекта (см. `../llm/schemas.md`
вступление): эти ID **всегда** генерируются кодом, никогда не заполняются
LLM — LLM ссылается на элементы только по локальному индексу в промпте
(`unit_index`, `item.index`), а код-обвязка роли сам подставляет реальный ID.

---

## 1. Task / Plan

### 1.1. `class Task(BaseModel)`

Исходный запрос пользователя, нормализованный кодом (не LLM). Создаётся в
`Orchestrator._load_or_create_state()` (`../orchestrator/state_machine.md`)
для новой задачи, либо восстанавливается из `TaskCheckpoint` при resume (с
уже известным `task_id`).

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | `default_factory=_new_id`. Ключ каталога в `staging/`, `checkpoints/`. При resume передаётся явно (не генерируется заново). |
| `raw_query` | `str` | Обязательное — исходный текст запроса пользователя. |
| `language` | `str` | Дефолт `"ru"`. Из `settings.language`. |
| `created_at` | `str` | `default_factory=_now`. |

### 1.2. `class OutlineSubpoint(BaseModel)`

Один подпункт (будущий заголовок `##`) внутри заметки плана. Единица
батчинга для `roles/elaborator.py` (`../roles/elaborator.md`).

| Поле | Тип | Назначение |
|---|---|---|
| `subpoint_id` | `str` | `default_factory=_new_id`. Стабильный ID для resume. |
| `heading` | `str` | Текст заголовка `##` в итоговой заметке. |
| `covers` | `str` | Техзадание для Elaborator/Writer — ЧТО раскрыть, **не сам текст**. |

### 1.3. `class OutlineNote(BaseModel)`

Одна заметка будущего конспекта — узел дерева `Plan.notes`. Поля
`action`/`existing_path`/`folder` заполняются НЕ Planner-ом, а кодом роли
`roles/vault_analyst.py::resolve_notes_against_vault` (мутация на месте,
без отдельного LLM-вызова на само решение "create vs update" — LLM
привлекается только для "серой зоны", см. `../roles/vault_analyst.md`).

| Поле | Тип | Назначение |
|---|---|---|
| `note_id` | `str` | `default_factory=_new_id`. Связывает заметку с её `Evidence` и с `written_note_indices` чекпоинта (там индекс в `plan.notes`, не `note_id` напрямую). |
| `title` | `str` | Заголовок заметки — зафиксирован планом, Writer его не выбирает. |
| `subpoints` | `list[OutlineSubpoint]` | `default_factory=list`. |
| `rationale` | `str` | Дефолт `""`. Обоснование Planner-а, не используется дальше по пайплайну. |
| `action` | `Literal["create", "update"]` | Дефолт `"create"`. Заполняется `vault_analyst`. |
| `existing_path` | `str` | Дефолт `""`. Путь существующей заметки при `action="update"` — обязателен (`synthesizer_writer._to_draft_note` поднимет `ValueError`, если пуст). |
| `folder` | `str` | Дефолт `""`. Для `action="create"` — заполняется `vault_analyst._assign_folders_batch`. |

### 1.4. `class Plan(BaseModel)`

Результат работы Planner-а (`roles/outline_planner.py::build_plan`,
`../roles/outline_planner.md`) — дерево заметок с подпунктами. Дальше
мутируется `vault_analyst` и читается всеми последующими ролями
(`elaborator`, `synthesizer_writer`, `critic`).

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное — связь с `Task.task_id`. |
| `topic_title` | `str` | Обязательное — общее название темы. |
| `summary` | `str` | Дефолт `""`. Используется в CLI как корень дерева при показе плана. |
| `notes` | `list[OutlineNote]` | `default_factory=list`. |

**Примечание про `RESEARCH_MODE=web`:** файлы `roles/researcher.py`/
`extractor_critic.py` (не документируются в этом заходе, см.
`../CONTRIBUTING.md`) ссылаются на `Plan.subtopics`, которого в ТЕКУЩЕЙ
версии `Plan` нет (только `notes`) — признак незавершённой миграции
web-режима на новую структуру `OutlineNote`/`subpoints`.

---

## 2. Evidence

### 2.1. `class Evidence(BaseModel)`

Одно атомарное утверждение/факт/определение, привязанное к конкретному
разделу конкретной заметки плана. Единый формат для `RESEARCH_MODE=knowledge`
(`roles/elaborator.py`, `../roles/elaborator.md`) — это то, что позволяет
`roles/synthesizer_writer.py`/`roles/critic.py` не знать, в каком режиме
работает система.

| Поле | Тип | Назначение |
|---|---|---|
| `evidence_id` | `str` | `default_factory=_new_id`. На него ссылаются другие `Evidence` через списки противоречий, если такие резолвятся кодом роли. |
| `note_id` | `str` | Обязательное — `OutlineNote.note_id`. По этому полю `synthesizer_writer.write_note`/`critic.run_critic_cycle` фильтруют "только мои факты" из общего списка `evidence` задачи. |
| `subpoint_id` | `str` | Обязательное — `OutlineSubpoint.subpoint_id`, конкретный раздел внутри заметки. |
| `statement` | `str` | Обязательное — сам текст утверждения. |
| `source_id` | `str` | Дефолт `"model_knowledge"`. В knowledge-режиме — всегда константа `roles/elaborator.py::MODEL_KNOWLEDGE_SOURCE_ID`. |
| `confidence` | `float` | `ge=0.0, le=1.0`, дефолт `0.5`. Оценка достоверности от LLM. |
| `is_definition` | `bool` | Дефолт `False`. Является ли утверждение определением понятия. |
| `critic_note` | `str` | Дефолт `""`. Комментарий модели о сомнительности/противоречии (заполняет сам Elaborator в рамках своего вызова — не путать с ролью `roles/critic.py`, `../roles/critic.md`). |
| `verified` | `bool` | Дефолт `False`. В knowledge-режиме ВСЕГДА `False` — сознательно НЕ выставляется в `True` даже если `roles/critic.py` не нашёл проблем: тот Critic проверяет согласованность/полноту, а не фактическую верность против внешней истины, которой в этом режиме просто нет. |

---

## 3. Vault Analyst

### 3.1. `class ExistingNote(BaseModel)`

Существующая заметка Vault, найденная локальным retrieval-ом как
потенциальный дубликат/кандидат на дополнение. В текущем активном пути
(`roles/vault_analyst.py::resolve_notes_against_vault`) эта модель напрямую
не строится — используется облегчённый `RetrievalHit` (`retrieval/search.py`)
вместо неё; `ExistingNote` описана здесь как более полный/архивный формат
для будущего использования.

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Путь заметки внутри Vault. |
| `title` | `str` | Заголовок. |
| `frontmatter` | `dict` | `default_factory=dict`. |
| `tags` | `list[str]` | `default_factory=list`. |
| `summary` | `str` | Дефолт `""`. |
| `content_hash` | `str` | Дефолт `""`. Для сверки с индексом (`../vault/db.md`). |
| `similarity_score` | `float` | Дефолт `0.0`. |
| `matched_concept` | `str` | Дефолт `""`. |
| `decision` | `Literal["reuse", "extend", "distinct", "unknown"]` | Дефолт `"unknown"`. Тот же словарь решений, что у `../llm/schemas.md §3`, `DedupDecisionOutput.decision`. |

---

## 4. Synthesizer + Writer

### 4.1. `class NoteAction(str, Enum)`

Строковый Enum действия над заметкой Vault: `CREATE = "create"`,
`UPDATE = "update"`. Используется в `DraftNote.action`,
`StagingChangeset.creates`/`.updates` (разделение списков по значению),
`../vault/writer.md::VaultWriter.write_draft`.

### 4.2. `class DraftNote(BaseModel)`

Черновик одной заметки — итог работы `synthesizer_writer`/`critic`, единица
staging-changeset-а, единица, которую видит пользователь в diff
(`../staging/diff.md`) перед `approve`.

| Поле | Тип | Назначение |
|---|---|---|
| `draft_id` | `str` | `default_factory=_new_id`. |
| `note_id` | `str` | Дефолт `""`. Для traceability и валидации покрытия заголовков (`../validation/markdown_validator.md`) — пусто у объединённых черновиков (`../staging/draft_merge.md`), т.к. исходная заметка-план для объединённой структуры уже не актуальна. |
| `action` | `NoteAction` | Обязательное. |
| `path` | `str` | Для `CREATE` — новый путь; для `UPDATE` — путь существующей заметки. |
| `title` | `str` | Обязательное. |
| `folder` | `str` | Дефолт `""`. |
| `frontmatter` | `dict` | `default_factory=dict`. Ограничен на этапе рендера ключами `title`/`tags`/`created`/`source` (`../tools/markdown_tools.md`) — прочие ключи здесь могут временно присутствовать, но не попадут в итоговый Markdown. |
| `body_md` | `str` | Дефолт `""`. Тело заметки для `action=CREATE`. |
| `tags` | `list[str]` | `default_factory=list`. |
| `links_out` | `list[str]` | `default_factory=list`. Заголовки/пути других заметок (не URL) — для секции "## Связанные заметки". |
| `source_refs` | `list[str]` | `default_factory=list`. URL источников — только web-режим. |
| `append_section` | `str \| None` | Дефолт `None`. Для `action=UPDATE` — ЧТО именно добавляется, чтобы не перезаписывать весь файл. |
| `depth_hint` | `str` | Дефолт `"standard"`. Чисто для трассируемости/отладки — не участвует в валидации. |
| `critic_rounds` | `int` | Дефолт `0`. Сколько раз Critic попросил переписать эту заметку (`../roles/critic.md`). |
| `needs_review` | `bool` | Дефолт `False`. `True`, если после исчерпания `max_critic_rounds` критик всё ещё просил переписать — сигнал пользователю в `../staging/diff.md`. |

### 4.3. `class Relationship(BaseModel)`

Одна связь (wikilink/tag/backlink) между заметками — плоское представление
рёбер графа знаний, строится `synthesizer_writer.build_relationships`.

| Поле | Тип | Назначение |
|---|---|---|
| `from_note` | `str` | Путь заметки-источника ссылки. |
| `to_note` | `str` | Путь (или заголовок-fallback) заметки-цели. |
| `link_type` | `Literal["wikilink", "tag", "backlink"]` | Дефолт `"wikilink"`. |

---

## 5. Validation / Staging

### 5.1. `class ValidationIssue(BaseModel)`

Одна найденная проблема (детерминированной валидацией, `validation/*.py`,
без LLM).

| Поле | Тип | Назначение |
|---|---|---|
| `level` | `Literal["error", "warning"]` | `"error"` блокирует `approve`, `"warning"` — только информирует. |
| `code` | `str` | Машиночитаемый код проблемы (напр. `"broken_wikilink"`, `"empty_body"`). |
| `message` | `str` | Человекочитаемое сообщение (на русском). |
| `draft_id` | `str \| None` | Дефолт `None`. Ссылка на конкретный `DraftNote.draft_id`, если применимо. |

### 5.2. `class ValidationReport(BaseModel)`

Итог полного прогона валидации (`../validation/_index.md::run_validation`).

| Поле | Тип | Назначение |
|---|---|---|
| `ok` | `bool` | `True`, если НЕТ ни одной issue уровня `"error"`. Блокирует/разрешает `approve`. |
| `issues` | `list[ValidationIssue]` | `default_factory=list`. |

`errors` (property) → `list[ValidationIssue]` с `level == "error"`.
`warnings` (property) → `list[ValidationIssue]` с `level == "warning"`.

### 5.3. `class StagingChangeset(BaseModel)`

Единица staging — то, что предлагается применить к реальному Vault.
Сериализуется в `changeset.json` (`../staging/changeset.md`) и читается
обратно при `approve`.

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное. |
| `created_at` | `str` | `default_factory=_now`. |
| `creates` | `list[DraftNote]` | `default_factory=list`. Черновики с `action=CREATE`. |
| `updates` | `list[DraftNote]` | `default_factory=list`. Черновики с `action=UPDATE`. |
| `deletes` | `list[str]` | `default_factory=list`. Пути к удалению — в MVP по умолчанию ВСЕГДА пуст. |
| `relationships` | `list[Relationship]` | `default_factory=list`. |
| `validation` | `ValidationReport \| None` | Дефолт `None`. `None` означает "changeset не прошёл через `run_validation`" — `../staging/commit.md::commit_changeset` явно проверяет это и поднимает `CommitError`, если так. |

---

## 6. Orchestrator status / бюджет

### 6.1. `class LLMCallLog(BaseModel)`

Одна запись о совершённом (или неудачном) вызове LLM — элемент
`TaskStatus.llm_calls_log`. Создаётся `../orchestrator/budget.md::LLMBudget.register_call`.

| Поле | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли вызова. |
| `timestamp` | `str` | `default_factory=_now`. |
| `prompt_tokens_est` | `int` | Дефолт `0`. Не заполняется в текущей реализации `register_call` — зарезервировано. |
| `ok` | `bool` | Дефолт `True`. |
| `error` | `str \| None` | Дефолт `None`. |

### 6.2. `class TaskStatus(BaseModel)`

Единственный изменяемый (мутируемый на месте, передаётся ПО ССЫЛКЕ) объект,
который "путешествует" через весь цикл `Orchestrator → роль → LLMClient →
GroqClient` (`../flows/llm_cycle.md §3`).

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное. |
| `stage` | `str` | Дефолт `"created"`. Текущий этап workflow (`"planning"`, `"extracting"`, `"vault_analysis"`, `"synthesizing"`, `"validating"`, `"staged"`, `"stopped"`). |
| `llm_calls_used` | `int` | Дефолт `0`. Счётчик вызовов **ЭТОЙ СЕССИИ** (обнуляется даже при resume). |
| `llm_calls_log` | `list[LLMCallLog]` | `default_factory=list`. |
| `stopped_reason` | `str \| None` | Дефолт `None`. Текст исключения (`LLMFreeLimitReached`/`LLMTaskBudgetExceeded`) при управляемой остановке. |
| `finished` | `bool` | Дефолт `False`. `True`, если задача успешно дошла до этапа `staged`. |

**Где создаётся:** `Orchestrator._load_or_create_state()` — всегда новый
объект (`llm_calls_used=0`), даже при `resume_task_id` переданном.

---

## Сводная схема связей между моделями

```
Task ──task_id──► Plan ──notes──► OutlineNote ──subpoints──► OutlineSubpoint
                                       │  note_id/subpoint_id
                                       ▼
                                   Evidence (note_id, subpoint_id, source_id)
                                       │
                    (Vault Analyst мутирует OutlineNote.action/existing_path/folder)
                                       │
                                       ▼
                                 synthesizer_writer.write_note
                                       │
                                       ▼
                                  DraftNote (note_id, action, path, ...)
                                       │
                          build_relationships ──► Relationship
                                       │
                                StagingChangeset (creates/updates/deletes/relationships/validation)
                                       │
                                run_validation ──► ValidationReport ──► ValidationIssue[]
                                       │
                             save_changeset (staging/) ──► approve ──► commit_changeset

TaskStatus — сквозной объект, передаётся во ВСЕ LLM-вызовы, накапливает LLMCallLog[]
```

Документация по `storage/models.py` завершена. Обзор пакета — `_index.md`.
