# Документация: `storage/models.py`

> Единственный модуль со структурированными объектами, которыми обмениваются
> этапы workflow (`Orchestrator → roles/* → validation/* → staging/*`).
> Правило проекта (см. докстринг модуля): между ролями никогда не передаётся
> длинный "сырой" текст — только эти типизированные Pydantic-модели. Это даёт:
> (1) валидацию на границах между ролями, (2) сериализацию в JSON и
> персистентность прогресса на диск (`staging/checkpoint.py` — resume после
> исчерпания лимита LLM-провайдера), (3) предсказуемый contract для
> structured-output вызовов LLM (см. `llm/schemas.py`, отдельные "выходные"
> Pydantic-модели именно под ответы LLM, конвертируемые в объекты отсюда
> кодом ролей — LLM никогда не заполняет эти модели напрямую).

---

## 0. Служебные функции модуля

### 0.1. `_now() -> str`

**Описание.** Текущее время в UTC, ISO 8601 (`datetime.now(timezone.utc).isoformat()`).
Используется как `default_factory` для полей `created_at`/`timestamp`.

**Параметры:** нет.
**Возвращаемое значение:** `str` — например `"2026-09-26T12:34:56.789012+00:00"`.
**Исключения:** не поднимает.

### 0.2. `_new_id() -> str`

**Описание.** Генерирует короткий уникальный идентификатор: первые 12 hex-символов
UUID4 (`uuid.uuid4().hex[:12]`). Используется как `default_factory` для всех
`*_id` полей (`task_id`, `note_id`, `subpoint_id`, `source_id`, `evidence_id`,
`draft_id`). Ключевой архитектурный принцип проекта (см. `llm/schemas.py`
докстринг): эти ID **всегда** генерируются кодом, никогда не заполняются LLM —
LLM ссылается на элементы только по локальному индексу в промпте (`unit_index`,
`item.index`), а код-обвязка роли сам подставляет реальный ID.

**Параметры:** нет.
**Возвращаемое значение:** `str` — 12 hex-символов, например `"a3f9c1d02e77"`.
**Исключения:** не поднимает.

---

## 1. Task / Plan

### 1.1. `class Task(BaseModel)`

**Описание.** Исходный запрос пользователя, нормализованный кодом (не LLM).
Создаётся в `Orchestrator._load_or_create_state()` для новой задачи, либо
восстанавливается из `TaskCheckpoint` при resume (с уже известным `task_id`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | `default_factory=_new_id`. Уникальный идентификатор задачи — используется как ключ каталога в `staging/`, `checkpoints/`. При resume передаётся явно (не генерируется заново). |
| `raw_query` | `str` | Обязательное поле — исходный текст запроса пользователя на естественном языке. |
| `language` | `str` | Дефолт `"ru"`. Из `settings.language`. |
| `created_at` | `str` | `default_factory=_now`. |

**Исключения:** стандартная Pydantic-валидация (`ValidationError`) при некорректных типах/отсутствии `raw_query`.

### 1.2. `class OutlineSubpoint(BaseModel)`

**Описание.** Один подпункт (будущий заголовок `##`) внутри заметки плана.
Единица батчинга для `roles/elaborator.py` (`ElaborationUnit.subpoint`) и
`roles/extractor_critic.py` (косвенно, через концепции).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `subpoint_id` | `str` | `default_factory=_new_id`. Стабильный ID для resume (`checkpoint.extracted_unit_ids`/`checkpoint.written_note_indices` работают через него). |
| `heading` | `str` | Текст заголовка `##` в итоговой заметке. |
| `covers` | `str` | Техзадание для Elaborator/Writer — ЧТО должно быть раскрыто в этом разделе, **не сам текст**. |

### 1.3. `class OutlineNote(BaseModel)`

**Описание.** Одна заметка будущего конспекта — узел дерева `Plan.notes`.
Поля `action`/`existing_path`/`folder` заполняются НЕ Planner-ом, а кодом роли
`roles/vault_analyst.py::resolve_notes_against_vault` (мутация на месте, без
отдельного LLM-вызова на само решение "create vs update" — LLM привлекается
только для "серой зоны", см. `docs_roles_part2.md §2`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `note_id` | `str` | `default_factory=_new_id`. Связывает заметку с её `Evidence` (`Evidence.note_id`) и с записями чекпоинта (`written_note_indices` — индекс в `plan.notes`, а не `note_id` напрямую, см. `staging/checkpoint.py`). |
| `title` | `str` | Заголовок заметки — зафиксирован планом, Writer его не выбирает (см. `llm/prompts/synthesizer_writer.py::WRITE_SYSTEM_INSTRUCTION`). |
| `subpoints` | `list[OutlineSubpoint]` | `default_factory=list`. Дерево разделов заметки. |
| `rationale` | `str` | Дефолт `""`. Обоснование Planner-а, почему эта заметка выделена отдельно — не используется дальше по пайплайну, только для диагностики/будущего UI. |
| `action` | `Literal["create", "update"]` | Дефолт `"create"`. Заполняется `vault_analyst` (см. выше). |
| `existing_path` | `str` | Дефолт `""`. Путь существующей заметки Vault при `action="update"` — обязателен в этом случае (`synthesizer_writer._to_draft_note` поднимет `ValueError`, если пуст). |
| `folder` | `str` | Дефолт `""`. Папка для `action="create"` — заполняется `vault_analyst._assign_folders_batch`. |

### 1.4. `class Plan(BaseModel)`

**Описание.** Результат работы Planner-а (`roles/outline_planner.py::build_plan`) —
дерево заметок с их подпунктами. Дальше мутируется `vault_analyst` (проставляет
`action`/`existing_path`/`folder` на местах) и читается всеми последующими
ролями (`elaborator`, `synthesizer_writer`, `critic`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное — связь с `Task.task_id`. |
| `topic_title` | `str` | Обязательное — общее название темы (из ответа Planner-а). |
| `summary` | `str` | Дефолт `""`. Краткое описание темы — используется в CLI (`cli/plan_editor.py::build_plan_tree`) как корень дерева при показе плана пользователю. |
| `notes` | `list[OutlineNote]` | `default_factory=list`. |

**Примечание про `roles/researcher.py`/`extractor_critic.py`:** эти файлы
ссылаются на `Plan.subtopics` (список подтем с `search_queries`), которого в
ТЕКУЩЕЙ версии `Plan` НЕТ (только `notes`) — это признак незавершённой
миграции `RESEARCH_MODE=web` на новую структуру `OutlineNote`/`subpoints`
(см. `docs_roles_part1.md §2-3` и `orchestrator/state_machine.py`, которая
явно блокирует `research_mode="web"`).

---

## 2. Research (только `RESEARCH_MODE=web`)

### 2.1. `class SourceCandidate(BaseModel)`

**Описание.** Один кандидат-источник — от сырого результата веб-поиска до
скачанного и очищенного текста. Один и тот же объект последовательно
обогащается на разных этапах (`tools/web_search.py` → `roles/researcher.py`
→ `tools/web_fetch.py`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `source_id` | `str` | `default_factory=_new_id`. Стабильный ID — на него ссылается `Evidence.source_id` и `ExtractionUnit.unit_id` (`f"{source_id}#{chunk_index}"`). |
| `url` | `str` | Обязательное. |
| `title` | `str` | Дефолт `""`. Заголовок страницы (из сниппета поиска). |
| `snippet` | `str` | Дефолт `""`. Сниппет поисковой выдачи. |
| `subtopic` | `str` | Дефолт `""`. К какой подтеме плана относится (проставляется `collect_raw_candidates`). |
| `relevance_score` | `float` | Дефолт `0.0`. Заполняется `select_relevant_sources` из ответа LLM. |
| `selected` | `bool` | Дефолт `False`. `True`, если кандидат прошёл финальный отбор `select_relevant_sources`. |
| `fetched_text` | `str \| None` | Дефолт `None`. Очищенный текст страницы (после `tools/web_fetch.py::fetch_clean_text`). |
| `fetch_error` | `str \| None` | Дефолт `None`. Текст ошибки, если fetch не удался. |

---

## 3. Evidence (общий формат для Extractor+Critic и Elaborator)

### 3.1. `class Evidence(BaseModel)`

**Описание.** Одно атомарное утверждение/факт/определение, привязанное к
конкретному разделу конкретной заметки плана. Единый формат для ОБОИХ режимов
исследования (`RESEARCH_MODE=web` → `roles/extractor_critic.py`,
`RESEARCH_MODE=knowledge` → `roles/elaborator.py`) — это то, что позволяет
`roles/synthesizer_writer.py`/`roles/critic.py` не знать, в каком режиме
работает система (см. `docs/architecture.md §2.1`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `evidence_id` | `str` | `default_factory=_new_id`. На него ссылаются другие `Evidence.contradicts` (список ID). |
| `note_id` | `str` | Обязательное — `OutlineNote.note_id`, к которой относится факт. Именно по этому полю `synthesizer_writer.write_note`/`critic.run_critic_cycle` фильтруют "только мои факты" из общего списка `evidence` задачи. |
| `subpoint_id` | `str` | Обязательное — `OutlineSubpoint.subpoint_id`, конкретный раздел внутри заметки. |
| `statement` | `str` | Обязательное — сам текст утверждения. |
| `source_id` | `str` | Дефолт `"model_knowledge"`. В web-режиме — реальный `SourceCandidate.source_id`; в knowledge-режиме — всегда константа `roles/elaborator.py::MODEL_KNOWLEDGE_SOURCE_ID`. |
| `confidence` | `float` | `ge=0.0, le=1.0`, дефолт `0.5`. Оценка достоверности от LLM. |
| `is_definition` | `bool` | Дефолт `False`. Является ли утверждение определением понятия (а не просто фактом). |
| `critic_note` | `str` | Дефолт `""`. Комментарий модели о сомнительности/противоречии утверждения (не путать с ролью `roles/critic.py` — здесь это заполняет сам Extractor/Elaborator в рамках своего вызова). |
| `verified` | `bool` | Дефолт `False`. `True` ТОЛЬКО в `RESEARCH_MODE=web`, если факт получен из реального внешнего источника. В knowledge-режиме ВСЕГДА `False` — сознательно НЕ выставляется в `True` даже если `roles/critic.py` не нашёл проблем: тот Critic проверяет согласованность/полноту, а не фактическую верность против внешней истины, которой в этом режиме просто нет. |
| `contradicts` | (не показано в текущем коде явно как поле верхнего уровня — см. примечание ниже) | — |

**Примечание:** в `llm/schemas.py::EvidenceItem` есть `contradicts_indices`
(индексы других утверждений в том же батче), которые `roles/extractor_critic.py::_to_evidence_list`
резолвит в реальные `evidence_id` — однако в приведённой версии
`storage/models.py::Evidence` явного поля `contradicts: list[str]` в тексте
модели не объявлено; при обращении к этому полю в коде ролей ориентируйтесь на
актуальную версию `storage/models.py` в репозитории.

---

## 4. Vault Analyst

### 4.1. `class ExistingNote(BaseModel)`

**Описание.** Существующая заметка Vault, найденная локальным retrieval-ом
(`retrieval/search.py::VaultSearcher`) как потенциальный дубликат/кандидат на
дополнение. В текущем активном пути (`roles/vault_analyst.py::resolve_notes_against_vault`)
эта модель напрямую не строится — используется облегчённый `RetrievalHit`
(`retrieval/search.py`) вместо неё; `ExistingNote` описана в `storage/models.py`
как более полный/архивный формат для будущего использования (например, для
`RESEARCH_MODE=web`, где `orchestrator/state_machine.py::TaskCheckpoint.existing_notes`
типизирован именно этим классом).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Путь заметки внутри Vault. |
| `title` | `str` | Заголовок. |
| `frontmatter` | `dict` | `default_factory=dict`. |
| `tags` | `list[str]` | `default_factory=list`. |
| `summary` | `str` | Дефолт `""`. |
| `content_hash` | `str` | Дефолт `""`. Для сверки с индексом (`vault/db.py::note_exists_with_hash`). |
| `similarity_score` | `float` | Дефолт `0.0`. |
| `matched_concept` | `str` | Дефолт `""`. |
| `decision` | `Literal["reuse", "extend", "distinct", "unknown"]` | Дефолт `"unknown"`. Тот же словарь решений, что у `llm/schemas.py::DedupDecisionOutput.decision`. |

---

## 5. Synthesizer + Writer

### 5.1. `class NoteAction(str, Enum)`

**Описание.** Строковый Enum действия над заметкой Vault.

**Значения:**

| Имя | Значение |
|---|---|
| `CREATE` | `"create"` |
| `UPDATE` | `"update"` |

**Где используется:** `DraftNote.action`, `StagingChangeset.creates`/`.updates`
(разделение списков по значению), `vault/writer.py::VaultWriter.write_draft`.

### 5.2. `class DraftNote(BaseModel)`

**Описание.** Черновик одной заметки — итог работы `synthesizer_writer`/`critic`,
единица staging-changeset-а, единица, которую видит пользователь в diff
(`staging/diff.py`) перед `approve`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `draft_id` | `str` | `default_factory=_new_id`. |
| `note_id` | `str` | Дефолт `""`. Для traceability и валидации покрытия заголовков (`validation/markdown_validator.py::validate_headings_coverage`) — пусто у объединённых черновиков (`staging/draft_merge.py::merge_drafts`, где исходная заметка-план для объединённой структуры уже не актуальна, проверка просто не выполняется). |
| `action` | `NoteAction` | Обязательное. |
| `path` | `str` | Для `CREATE` — новый путь; для `UPDATE` — путь существующей заметки. |
| `title` | `str` | Обязательное. |
| `folder` | `str` | Дефолт `""`. |
| `frontmatter` | `dict` | `default_factory=dict`. Ограничен на этапе рендера ключами `title`/`tags`/`created`/`source` (см. `tools/markdown_tools.py::_ALLOWED_FRONTMATTER_KEYS`) — прочие ключи здесь могут временно присутствовать, но не попадут в итоговый Markdown. |
| `body_md` | `str` | Дефолт `""`. Тело заметки для `action=CREATE`. |
| `tags` | `list[str]` | `default_factory=list`. |
| `links_out` | `list[str]` | `default_factory=list`. Заголовки/пути других заметок (не URL) — для секции "## Связанные заметки". |
| `source_refs` | `list[str]` | `default_factory=list`. URL источников — для секции "## Источники" в начале тела (только web-режим). |
| `append_section` | `str \| None` | Дефолт `None`. Для `action=UPDATE` — ЧТО именно добавляется (append-блок), чтобы не перезаписывать весь файл. |
| `depth_hint` | `str` | Дефолт `"standard"`. Чисто для трассируемости/отладки — не участвует в валидации (единый порог глубины для всех заметок). |
| `critic_rounds` | `int` | Дефолт `0`. Сколько раз Critic попросил переписать эту заметку и Writer переписал её заново (см. `roles/critic.py::run_critic_cycle`). |
| `needs_review` | `bool` | Дефолт `False`. `True`, если после исчерпания `max_critic_rounds` критик всё ещё просил переписать — сигнал пользователю в `staging/diff.py`. |

### 5.3. `class Relationship(BaseModel)`

**Описание.** Одна связь (wikilink/tag/backlink) между заметками — плоское
представление рёбер графа знаний, строится `synthesizer_writer.build_relationships`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `from_note` | `str` | Путь заметки-источника ссылки. |
| `to_note` | `str` | Путь (или заголовок-fallback, если путь неизвестен) заметки-цели. |
| `link_type` | `Literal["wikilink", "tag", "backlink"]` | Дефолт `"wikilink"`. |

---

## 6. Validation / Staging

### 6.1. `class ValidationIssue(BaseModel)`

**Описание.** Одна найденная проблема (детерминированной валидацией,
`validation/*.py`, без LLM).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `level` | `Literal["error", "warning"]` | `"error"` блокирует `approve`, `"warning"` — только информирует. |
| `code` | `str` | Машиночитаемый код проблемы (напр. `"broken_wikilink"`, `"empty_body"`, `"create_collides_with_existing"`). |
| `message` | `str` | Человекочитаемое сообщение (на русском), показывается в `staging/diff.py`. |
| `draft_id` | `str \| None` | Дефолт `None`. Ссылка на конкретный `DraftNote.draft_id`, если применимо (некоторые issues — на уровне всего changeset-а, напр. `delete_not_allowed`). |

### 6.2. `class ValidationReport(BaseModel)`

**Описание.** Итог полного прогона валидации (`validation/__init__.py::run_validation`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `ok` | `bool` | `True`, если НЕТ ни одной issue уровня `"error"`. Блокирует/разрешает `approve`. |
| `issues` | `list[ValidationIssue]` | `default_factory=list`. Все найденные проблемы, и ошибки, и предупреждения. |

#### `errors` (property) `-> list[ValidationIssue]`

**Описание.** Отфильтрованный список issues с `level == "error"`.
**Возвращаемое значение:** `list[ValidationIssue]`.

#### `warnings` (property) `-> list[ValidationIssue]`

**Описание.** Отфильтрованный список issues с `level == "warning"`.
**Возвращаемое значение:** `list[ValidationIssue]`.

### 6.3. `class StagingChangeset(BaseModel)`

**Описание.** Единица staging — то, что предлагается применить к реальному
Vault. Сериализуется в `changeset.json` (`staging/changeset.py::save_changeset`)
и читается обратно при `approve` (`cli/main.py::approve` → `load_changeset` →
`staging/commit.py::commit_changeset`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное. |
| `created_at` | `str` | `default_factory=_now`. |
| `creates` | `list[DraftNote]` | `default_factory=list`. Черновики с `action=CREATE`. |
| `updates` | `list[DraftNote]` | `default_factory=list`. Черновики с `action=UPDATE`. |
| `deletes` | `list[str]` | `default_factory=list`. Пути к удалению — в MVP по умолчанию ВСЕГДА пуст (см. `settings.allow_delete`). |
| `relationships` | `list[Relationship]` | `default_factory=list`. |
| `validation` | `ValidationReport \| None` | Дефолт `None`. `None` означает "changeset не прошёл через `run_validation`" — `staging/commit.py::commit_changeset` явно проверяет `changeset.validation is None or not changeset.validation.ok` и поднимает `CommitError`, если так. |

---

## 7. Orchestrator status / бюджет

### 7.1. `class LLMCallLog(BaseModel)`

**Описание.** Одна запись о совершённом (или неудачном) вызове LLM —
элемент `TaskStatus.llm_calls_log`. Создаётся `orchestrator/budget.py::LLMBudget.register_call`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли вызова (`"critic"`, `"synthesizer_write"` и т.п.). |
| `timestamp` | `str` | `default_factory=_now`. |
| `prompt_tokens_est` | `int` | Дефолт `0`. Не заполняется в текущей реализации `register_call` (поле зарезервировано, реальная оценка токенов живёт внутри `GroqClient`/`TokenEstimateCalibrator`, а не здесь). |
| `ok` | `bool` | Дефолт `True`. Успешно ли завершился вызов. |
| `error` | `str \| None` | Дефолт `None`. Текст ошибки, если `ok=False`. |

### 7.2. `class TaskStatus(BaseModel)`

**Описание.** Единственный изменяемый (мутируемый на месте, передаётся ПО
ССЫЛКЕ) объект, который "путешествует" через весь цикл
`Orchestrator → роль → LLMClient → GroqClient` (см. `docs_llm_cycle_part1_orchestrator.md §6`).

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Обязательное. |
| `stage` | `str` | Дефолт `"created"`. Текущий этап workflow (`"planning"`, `"extracting"`, `"vault_analysis"`, `"synthesizing"`, `"validating"`, `"staged"`, `"stopped"`). |
| `llm_calls_used` | `int` | Дефолт `0`. Счётчик вызовов **ЭТОЙ СЕССИИ** (обнуляется даже при resume — см. `orchestrator/state_machine.py::_load_or_create_state`). |
| `llm_calls_log` | `list[LLMCallLog]` | `default_factory=list`. |
| `stopped_reason` | `str \| None` | Дефолт `None`. Заполняется текстом исключения (`LLMFreeLimitReached`/`LLMTaskBudgetExceeded`) при управляемой остановке. |
| `finished` | `bool` | Дефолт `False`. `True`, если задача успешно дошла до этапа `staged`. |

**Где создаётся:** `Orchestrator._load_or_create_state()` — всегда НОВЫЙ объект
(`llm_calls_used=0`), даже при `resume_task_id` переданном.

---

## Сводная схема связей между моделями

```
Task ──task_id──► Plan ──notes──► OutlineNote ──subpoints──► OutlineSubpoint
                                       │  note_id/subpoint_id
                                       ▼
                                   Evidence (note_id, subpoint_id, source_id, contradicts)
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
                             save_changeset (staging/) ──► approve ──► commit_changeset (vault/writer.py)

TaskStatus — сквозной объект, передаётся во ВСЕ LLM-вызовы, накапливает LLMCallLog[]
```

Документация по `storage/models.py` завершена.

---

## Что дальше

Следующий файл (`docs_staging.md`) опишет папку `staging/` целиком:
`staging/changeset.py`, `staging/checkpoint.py`, `staging/commit.py`,
`staging/diff.py`, `staging/draft_merge.py` — как `StagingChangeset` и
`TaskCheckpoint` сохраняются/загружаются с диска, как происходит `commit`
в реальный Vault, как строится diff для пользователя и как работает
опциональное объединение черновиков.
