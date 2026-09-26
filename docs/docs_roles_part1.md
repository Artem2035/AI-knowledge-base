# Документация: папка `roles/` — часть 1

> Роли — это НЕ отдельные "агенты" со своим циклом: каждая роль — это функция
> (иногда несколько), которая (1) собирает динамический промпт из уже готовых
> структурированных данных (`storage/models.py`), (2) вызывает
> `client.generate_structured(role=..., ...)` (см. `docs_llm_cycle_part1..3`),
> (3) конвертирует Pydantic-ответ модели (`llm/schemas.py`) обратно в объекты
> `storage/models.py`. Роли никогда не пишут в реальный Vault и не хранят
> состояние между вызовами — всё состояние живёт в объектах, которые им
> передаёт `Orchestrator` (`orchestrator/state_machine.py`).
>
> Эта часть охватывает: `roles/outline_planner.py`, `roles/researcher.py`,
> `roles/extractor_critic.py`. Часть 2 (`docs_roles_part2.md`) охватывает
> `roles/elaborator.py`, `roles/vault_analyst.py`, `roles/synthesizer_writer.py`,
> `roles/critic.py`.

---

## 0. `roles/__init__.py`

Пустой файл-маркер пакета. Ничего не экспортирует.

---

## 1. `roles/outline_planner.py` — роль Planner

**Назначение.** Единственная функция здесь превращает сырой запрос
пользователя (`Task.raw_query`) в структурированный план конспекта
(`Plan` с деревом `OutlineNote` → `OutlineSubpoint`). Это ПЕРВЫЙ LLM-вызов
задачи (`role="outline_planner"`) и самый дешёвый по объёму промпта.

### 1.1. `build_plan(task: Task, client: LLMClient, status: TaskStatus) -> Plan`

**Описание.** Строит промпт из `task.raw_query`, отправляет один
`generate_structured` вызов с системной инструкцией
`llm/prompts/outline_planner.py::OUTLINE_PLANNER_SYSTEM_INSTRUCTION`
(правила: заметка = самостоятельная атомарная концепция; число заметок и
подпунктов определяется полнотой темы, а не круглым числом; для каждого
подпункта — короткое техзадание `covers`, а не сам текст; порядок
подпунктов — определение → механизм → применение → ограничения, где
уместно). Возвращает распарсенный `OutlinePlanOutput` (`llm/schemas.py`)
и конвертирует его в доменный объект `Plan`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `task` | `Task` (`storage/models.py`) | Источник `task.task_id` (переносится в `Plan.task_id`) и `task.raw_query` (подставляется в промпт как есть, в кавычках через `!r`). |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm` (не extraction-клиент — planning дешёвый и нечастый). |
| `status` | `TaskStatus` | Передаётся насквозь в `client.generate_structured(...)` для учёта бюджета. |

**Возвращаемое значение:** `Plan` —
```python
Plan(
    task_id=task.task_id,
    topic_title=output.topic_title,
    summary=output.summary,
    notes=[OutlineNote(title=n.title, rationale=n.rationale,
                        subpoints=[OutlineSubpoint(heading=sp.heading, covers=sp.covers) for sp in n.subpoints])
           for n in output.notes],
)
```
Каждый `OutlineNote`/`OutlineSubpoint` получает свой `note_id`/`subpoint_id`
автоматически (`default_factory=_new_id` в `storage/models.py`) — Planner
их не назначает, это ответственность кода, не модели (см. `storage/models.py`
о принципе "foreign keys не должны придумываться LLM").

**Исключения:** любые, поднимаемые `client.generate_structured(...)`
(`LLMTaskBudgetExceeded`, `LLMFreeLimitReached`, `GroqPromptTooLargeError`,
`GroqSchemaError` и т.д. — см. документацию `GroqClient`) — эта функция их
не перехватывает, они всплывают до `Orchestrator.run()`.

**Тег роли для GroqClient:** `role="outline_planner"`.

---

## 2. `roles/researcher.py` — роль Researcher (только `RESEARCH_MODE=web`)

**Назначение.** Собирает и отбирает внешние источники для дальнейшего
извлечения фактов (`extractor_critic.py`). Активна ТОЛЬКО в
`RESEARCH_MODE=web` — в дефолтном `RESEARCH_MODE=knowledge` этот файл не
вызывается вообще (Orchestrator в knowledge-режиме идёт сразу к
`elaborator.py`, см. Часть 1 документации цикла и `docs/architecture.md §2.1`).
Веб-поиск сам по себе — не LLM-вызов (используется бесплатный DuckDuckGo
через `ddgs`, `tools/web_search.py`); LLM нужен только для отбора релевантных
кандидатов из уже найденных сниппетов.

**Важно (текущее состояние проекта):** `orchestrator/state_machine.py::run()`
на данный момент явно поднимает `OrchestratorStopped` при
`settings.research_mode == "web"` ("ещё не мигрирован на новую структуру
плана OutlineNote/subpoints") — то есть код `researcher.py` существует, но
Orchestrator его сейчас не вызывает. Документируется как есть, на случай
восстановления web-режима.

### 2.1. `SYSTEM_INSTRUCTION: str`

Статичная системная инструкция для отбора источников (`role="researcher_selection"`):
предпочитать официальную документацию/научные статьи/авторитетные технические
ресурсы, исключать спам/рекламу/дубликаты/форумы низкого качества, оценивать
`relevance_score` от 0 до 1.

### 2.2. `collect_raw_candidates(plan: Plan, max_results_per_query: int, max_sources_per_subtopic: int) -> list[SourceCandidate]`

**Описание.** ЧИСТЫЙ КОД, без LLM. Для каждой подтемы плана (`plan.subtopics`)
и каждого поискового запроса подтемы (`subtopic.search_queries`) вызывает
`tools/web_search.py::search_web(...)`, дедуплицирует по URL
(`tools/web_search.py::deduplicate_by_url`), обрезает список до
`max_sources_per_subtopic * 2` кандидатов на подтему (запас "на вырост" до
финального отбора LLM).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник `plan.subtopics` (у каждой подтемы — `.title` и `.search_queries`). ⚠ Поле `Plan.subtopics` в текущей версии `storage/models.py` отсутствует (модель Plan имеет `notes`, а не `subtopics`) — это признак незавершённой миграции web-режима, упомянутой выше. |
| `max_results_per_query` | `int` | Сколько результатов запрашивать у `search_web` на один поисковый запрос. Из `settings.max_search_results_per_query`. |
| `max_sources_per_subtopic` | `int` | Верхний потолок кандидатов на подтему ПОСЛЕ дедупликации (с запасом `*2`, финальный отбор — ниже). Из `settings.max_sources_per_subtopic`. |

**Возвращаемое значение:** `list[SourceCandidate]` — объединённый список по всем подтемам.

**Исключения:** не поднимает намеренно — `tools/web_search.py::search_web`
сама ловит сетевые ошибки и логирует предупреждение, возвращая пустой список
при сбое (см. `tools/web_search.py`).

### 2.3. `select_relevant_sources(candidates: list[SourceCandidate], client: LLMClient, status: TaskStatus, max_per_subtopic: int) -> list[SourceCandidate]`

**Описание.** Единственный LLM-зависимый шаг роли. Делит `candidates` на
батчи под токен-бюджет клиента (`llm/chunking.py::split_items_into_batches`,
см. документацию цикла Часть 3 §6.3 — использует
`client.available_prompt_budget_tokens(...)`), на каждый батч делает один
вызов `generate_structured(role="researcher_selection", ...)`, помечает
принятые кандидаты (`cand.selected = True`, `cand.relevance_score = item.relevance_score`)
и ограничивает результат до `max_per_subtopic` источников на подтему (после
сортировки по убыванию `relevance_score`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `candidates` | `list[SourceCandidate]` | Сырые кандидаты из `collect_raw_candidates(...)`. |
| `client` | `LLMClient` | LLM-клиент для отбора (обычно `Orchestrator.self.llm`). |
| `status` | `TaskStatus` | Учёт бюджета. |
| `max_per_subtopic` | `int` | Финальный потолок источников на подтему. Из `settings.max_sources_per_subtopic`. |

**Возвращаемое значение:** `list[SourceCandidate]` — только те, что модель
пометила `keep=True`, отсортированные и обрезанные по `max_per_subtopic` на
подтему (`by_subtopic_count` — локальный счётчик внутри функции). Если
`candidates` пуст — возвращает `[]` без LLM-вызова.

**Исключения:** те же, что у `client.generate_structured(...)` — не перехватываются.

**Тег роли для GroqClient:** `role="researcher_selection"`.

**Важный нюанс батчинга:** индекс `item.index` в ответе модели — ЛОКАЛЬНЫЙ
для конкретного батча (0-based внутри `batch`, не глобальный offset по всему
списку `candidates`) — резолвится через `batch[item.index]` внутри цикла по
батчам.

### 2.4. `fetch_selected_sources(selected: list[SourceCandidate]) -> list[SourceCandidate]`

**Описание.** ЧИСТЫЙ КОД, без LLM. Для каждого отобранного кандидата
скачивает и очищает текст страницы через `tools/web_fetch.py::fetch_clean_text(cand.url)`,
записывает результат в `cand.fetched_text`/`cand.fetch_error`, логирует
неудачные fetch (`logger.info`), возвращает только те, у кого
`fetched_text` не пуст.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `selected` | `list[SourceCandidate]` | Результат `select_relevant_sources(...)`. |

**Возвращаемое значение:** `list[SourceCandidate]` — подмножество `selected`
с успешно скачанным текстом (`c.fetched_text` truthy).

**Исключения:** не поднимает — `fetch_clean_text` сама ловит HTTP/extract
ошибки и возвращает `(None, error_text)`.

---

## 3. `roles/extractor_critic.py` — роль Extractor+Critic (только `RESEARCH_MODE=web`)

**Назначение.** Объединённая роль: из текста УЖЕ СКАЧАННЫХ источников
(`SourceCandidate.fetched_text`) извлекает атомарные факты/определения/тезисы
(`Evidence`), одновременно оценивая их достоверность (`confidence`,
`critic_note`) и фиксируя противоречия между утверждениями РАЗНЫХ источников
(`contradicts`). Как и `researcher.py`, активна только в `RESEARCH_MODE=web`
(в текущей версии Orchestrator этот путь не вызывается — см. предупреждение
выше). В `RESEARCH_MODE=knowledge` её эквивалент — `roles/elaborator.py`
(Часть 2).

### 3.1. `SYSTEM_INSTRUCTION: str`

Статичная инструкция (`role="extractor_critic"`): извлечь атомарные
утверждения, привязать каждое к `unit_index` (номер "единицы" текста внутри
батча — см. §3.3 ниже), оценить `confidence` по качеству источника/ясности
формулировки, заполнить `contradicts_indices` при противоречиях МЕЖДУ
единицами, не включать маркетинговые утверждения/воду.

### 3.2. Константы модуля

| Имя | Значение | Назначение |
|---|---|---|
| `_UNIT_TARGET_CHARS` | `3000` | Целевой размер ОДНОЙ "единицы" (чанка одного источника) — намеренно меньше типичного TPM-бюджета одного вызова, чтобы несколько единиц из разных источников помещались в один батч. Статическая константа (не запрос текущего бюджета клиента) — иначе `unit_id` "плыл" бы между сессиями resume. |
| `_MIN_UNIT_CHARS` | `1200` | Нижний порог размера единицы — не используется напрямую в `_split_text` в текущей реализации, задокументирован как ориентир для будущей рекурсивной бисекции по размеру. |
| `_MAX_SPLIT_DEPTH` | `2` | Сколько раз пробуется рекурсивное деление батча пополам при `GroqSchemaError`, прежде чем батч пропускается с предупреждением. |

### 3.3. `class ExtractionUnit` (dataclass)

**Описание.** Один чанк текста одного источника — минимальная единица
батчинга извлечения. НЕ Pydantic-модель (обычный `@dataclass`), т.к. не
пересекает границу LLM-контракта (не сериализуется в JSON Schema) и не
персистится напрямую — персистится только `unit_id` (см.
`staging/checkpoint.py::TaskCheckpoint.extracted_unit_ids`).

**Поля:**

| Имя | Тип | Назначение |
|---|---|---|
| `source` | `SourceCandidate` | Источник, из которого взят чанк. |
| `chunk_text` | `str` | Сам текст чанка. |
| `chunk_index` | `int` | Порядковый номер чанка ВНУТРИ этого источника (0-based). |

#### `unit_id` (property) `-> str`

**Описание.** `f"{self.source.source_id}#{self.chunk_index}"` — стабильный
идентификатор единицы для resume-логики: используется, чтобы при
продолжении задачи не пересчитывать уже обработанные единицы.

### 3.4. `_split_text(text: str, chunk_chars: int) -> list[str]` (приватная)

**Описание.** Режет текст на чанки размером примерно `chunk_chars` символов,
стараясь не рвать посреди предложения — ищет ближайший `\n` или `. ` в
последних ~20% допустимой длины чанка (`search_from = end - chunk_chars*0.2`).
Если текст короче `chunk_chars` — возвращает его целиком одним элементом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `text` | `str` | Текст источника (уже обрезанный по `max_chars` в вызывающем коде). |
| `chunk_chars` | `int` | Целевой размер чанка в символах (`_UNIT_TARGET_CHARS`). |

**Возвращаемое значение:** `list[str]` — непустые чанки (пустые строки после `.strip()` отфильтрованы).

**Исключения:** не поднимает.

### 3.5. `build_extraction_units(sources: list[SourceCandidate], max_chars: int = 8000, max_units_per_source: int = 3) -> list[ExtractionUnit]`

**Описание.** ЧИСТЫЙ КОД, без LLM. Строит все `ExtractionUnit` для всех
источников с непустым `fetched_text`. Детерминированная функция (статическая
`_UNIT_TARGET_CHARS`, не зависящая от текущего состояния бюджета клиента) —
можно безопасно пересчитывать заново на каждом resume, `unit_id` не изменится.
Если после `_split_text` у источника получилось больше `max_units_per_source`
чанков — берутся только первые `max_units_per_source` (с предупреждением в
лог о проценте потерянного текста), остальной текст источника не обрабатывается.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `sources` | `list[SourceCandidate]` | Источники с уже скачанным текстом (`fetched_text`). Источники без текста пропускаются (`if not source.fetched_text: continue`). |
| `max_chars` | `int` (дефолт `8000`) | Сколько символов текста источника брать до нарезки на чанки (`source.fetched_text[:max_chars]`). Из `settings.max_sources_per_subtopic`-независимого параметра, передаётся явно вызывающим кодом (`Orchestrator`, если бы вызывал). |
| `max_units_per_source` | `int` (дефолт `3`) | Потолок чанков на один источник. Из `settings.max_chunks_per_source`. |

**Возвращаемое значение:** `list[ExtractionUnit]` — по всем источникам, в порядке `sources`, внутри источника — по `chunk_index`.

**Исключения:** не поднимает. Логирует `logger.warning(...)` при обрезке источника.

### 3.6. `extract_evidence_from_sources(sources, plan, client, status, already_done_unit_ids, on_batch_done, max_chars=8000, max_units_per_source=3) -> None`

**Описание.** Главная функция роли — извлекает evidence СРАЗУ из НЕСКОЛЬКИХ
источников за счёт межисточникового батчинга (в отличие от прежней версии, где
каждый источник обрабатывался изолированно и "недогруженный хвост" последнего
чанка источника тратил впустую место в TPM-окне). Строит все единицы
(`build_extraction_units`), исключает уже обработанные (`already_done_unit_ids`,
для resume), делит оставшиеся на батчи под токен-бюджет клиента
(`llm/chunking.py::split_items_into_batches`), на каждый батч вызывает
`_extract_from_batch(...)` и передаёт результат в callback `on_batch_done`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `sources` | `list[SourceCandidate]` | Источники для извлечения. |
| `plan` | `Plan` | Источник `plan.topic_title` и `plan.subtopics` (концепции, к которым привязываются факты) — подставляется в `static_overhead_text` для расчёта бюджета батча и в сам промпт `_extract_from_batch`. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.extraction_client` (отдельный бюджет/модель, см. документацию цикла Часть 1 §4.2). |
| `status` | `TaskStatus` | Учёт бюджета — общий на задачу, независимо от клиента. |
| `already_done_unit_ids` | `set[str]` | Множество `unit_id`, уже обработанных в предыдущих сессиях (для resume) — эти единицы исключаются из батчинга. |
| `on_batch_done` | `Callable[[list[str], list[Evidence]], None]` | Callback, вызываемый ПОСЛЕ КАЖДОГО батча с `(unit_ids батча, новый evidence)`. Вызывающий код (Orchestrator) ОБЯЗАН немедленно сохранить `unit_ids` в чекпоинт — иначе resume потеряет прогресс. |
| `max_chars` | `int` (дефолт `8000`) | Передаётся в `build_extraction_units`. |
| `max_units_per_source` | `int` (дефолт `3`) | Передаётся в `build_extraction_units`. |

**Возвращаемое значение:** `None` (результат передаётся через `on_batch_done`, не через `return`).

**Исключения:** не перехватывает верхнеуровнево — `_extract_from_batch` внутри
себя обрабатывает `GroqPromptTooLargeError`/`GroqSchemaError` через рекурсивное
деление батча (см. §3.7), остальные исключения клиента всплывают наружу.

**Побочный эффект при пустом `remaining_units`:** функция выходит немедленно
(`return`) без единого LLM-вызова, если все единицы уже обработаны.

### 3.7. `_extract_from_batch(units, plan, concepts, client, status, depth) -> list[Evidence]` (приватная)

**Описание.** Один LLM-вызов на один батч единиц (или рекурсивная бисекция при
сбое). Строит нумерованный листинг единиц (с `unit_index` в квадратных
скобках, заголовком источника и URL), делает
`client.generate_structured(role="extractor_critic", ...)`.

**Обработка ошибок:**
- `GroqPromptTooLargeError` — если в батче `len(units) <= 1`, единица
  пропускается с предупреждением (возвращает `[]`); иначе — батч делится
  пополам через `_split_batch_and_retry` (без увеличения `depth`, т.к. это не
  ошибка качества JSON, а просто нехватка места).
- `GroqSchemaError` — если `depth >= _MAX_SPLIT_DEPTH` или `len(units) <= 1` —
  батч пропускается с предупреждением, возвращает `[]`; иначе — деление пополам
  через `_split_batch_and_retry` с `depth + 1`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `units` | `list[ExtractionUnit]` | Единицы текущего батча (или под-батча при рекурсии). |
| `plan` | `Plan` | Для `plan.topic_title` в промпте. |
| `concepts` | `str` | Уже отрендеренная строка `", ".join(s.title for s in plan.subtopics)` — вычисляется один раз в `extract_evidence_from_sources` и передаётся дальше без пересчёта. |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `depth` | `int` | Текущая глубина рекурсии деления батча (для `GroqSchemaError`-ветки; `GroqPromptTooLargeError`-ветка НЕ увеличивает `depth`). |

**Возвращаемое значение:** `list[Evidence]` — пустой список, если `units` пуст с самого начала, либо если батч пришлось отбросить.

**Исключения:** не поднимает наружу — все ожидаемые ошибки клиента перехвачены и обработаны внутри (пропуск с логом или рекурсия). Непредвиденные исключения (не `GroqPromptTooLargeError`/`GroqSchemaError`) всплывают как есть.

### 3.8. `_split_batch_and_retry(units, plan, concepts, client, status, depth) -> list[Evidence]` (приватная)

**Описание.** Делит `units` пополам (`mid = len(units) // 2`), рекурсивно
вызывает `_extract_from_batch` на каждой половине, конкатенирует результаты.

**Параметры:** те же, что у `_extract_from_batch`.

**Возвращаемое значение:** `list[Evidence]` — `left + right`.

**Исключения:** пробрасывает то, что не поймано внутри рекурсивных вызовов `_extract_from_batch`.

### 3.9. `_to_evidence_list(output: EvidenceBatchOutput, units: list[ExtractionUnit]) -> list[Evidence]` (приватная)

**Описание.** ЧИСТЫЙ КОД — конвертирует сырой Pydantic-ответ модели
(`EvidenceBatchOutput` из `llm/schemas.py`) в доменные объекты `Evidence`.
Резолвит `item.unit_index` в реальный `source` конкретной единицы батча (если
индекс вне диапазона — приписывает утверждение первой единице батча, с
предупреждением в лог). Отдельным проходом резолвит `contradicts_indices`
(индексы ДРУГИХ утверждений ВНУТРИ ТОГО ЖЕ батча) в реальные `evidence_id`
через промежуточную мапу `id_by_index`, исключая самоссылки (`i != idx`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `output` | `EvidenceBatchOutput` | Сырой ответ модели (список `EvidenceItem`). |
| `units` | `list[ExtractionUnit]` | Единицы ТЕКУЩЕГО батча — для резолва `unit_index` → `source`. |

**Возвращаемое значение:** `list[Evidence]`, каждый со сгенерированным `evidence_id`
(`default_factory=_new_id`), `source_id` — реальный `source.source_id` единицы,
`contradicts` — список реальных `evidence_id` из ЭТОГО ЖЕ списка.

**Исключения:** не поднимает. Логирует `logger.warning(...)` при `unit_index` вне диапазона.

### 3.10. `extract_evidence_from_source(source, plan, client, status, max_chars=8000) -> list[Evidence]`

**Описание.** Совместимость с прежним интерфейсом (используется в
тестах/вызовах вне Orchestrator) — извлекает evidence из ОДНОГО источника, без
межисточникового батчинга. Реальный Orchestrator (если бы вызывал web-режим)
использует `extract_evidence_from_sources(...)` напрямую для нескольких
источников сразу. Внутри — тонкая обёртка: собирает результаты через локальный
callback `_collect` и вызывает `extract_evidence_from_sources([source], ...)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `source` | `SourceCandidate` | Единственный источник. |
| `plan` | `Plan` | См. выше. |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `max_chars` | `int` (дефолт `8000`) | См. `build_extraction_units`. |

**Возвращаемое значение:** `list[Evidence]` — весь накопленный evidence по этому источнику.

**Исключения:** те же, что у `extract_evidence_from_sources`.

---

## Что дальше (Часть 2 документации по `roles/`)

`docs_roles_part2.md` опишет:
- `roles/elaborator.py` — эквивалент Extractor+Critic для `RESEARCH_MODE=knowledge` (дефолтный режим);
- `roles/vault_analyst.py` — дедупликация с существующим Vault + распределение по папкам;
- `roles/synthesizer_writer.py` — генерация текста самой заметки (Writer);
- `roles/critic.py` — bounded-retry ревью уже написанной заметки.
