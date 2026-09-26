# Документация: папка `roles/` — часть 2

> Продолжение `docs_roles_part1.md` (`outline_planner`, `researcher`,
> `extractor_critic`). Здесь: `roles/elaborator.py`, `roles/vault_analyst.py`,
> `roles/synthesizer_writer.py`, `roles/critic.py` — активная цепочка ролей
> для дефолтного `RESEARCH_MODE=knowledge`.

---

## 1. `roles/elaborator.py` — роль Elaborator (замена Extractor+Critic для `RESEARCH_MODE=knowledge`)

**Назначение.** В отличие от `extractor_critic.py`, здесь НЕТ входного текста
источника — модель раскрывает каждый подпункт плана (`OutlineSubpoint`) из
СОБСТВЕННЫХ знаний. Результат — тот же тип объекта `Evidence`, что и у
Extractor+Critic (см. `storage/models.py::Evidence`), поэтому все следующие
этапы (`vault_analyst`, `synthesizer_writer`) не знают и не должны знать, в
каком режиме работает система (`docs/architecture.md §2.1`). Это САМЫЙ ЧАСТЫЙ
по числу вызовов шаг в knowledge-режиме — использует отдельный клиент
`Orchestrator.self.extraction_client`.

### `MODEL_KNOWLEDGE_SOURCE_ID: str = "model_knowledge"`

Константа — значение `Evidence.source_id` для ВСЕХ фактов, произведённых этой
ролью (т.к. у них нет реального внешнего источника). Совпадает по смыслу (но
не по значению написания) с `orchestrator/state_machine.py::KNOWLEDGE_MODE_FRONTMATTER_SOURCE
= "model-knowledge"` — первая маркирует `Evidence` (внутренний объект), вторая
— YAML frontmatter итоговой заметки (видимое пользователю значение).

### 1.1. `class ElaborationUnit` (dataclass)

**Описание.** Единица батчинга — ОДИН подпункт ОДНОЙ заметки плана.

**Поля:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана, к которой принадлежит подпункт. |
| `subpoint` | `OutlineSubpoint` | Сам подпункт (`heading`, `covers`, `subpoint_id`). |

### 1.2. `build_elaboration_units(plan: Plan, already_done_subpoint_ids: set[str]) -> list[ElaborationUnit]`

**Описание.** ЧИСТЫЙ КОД, без LLM. "Разворачивает" дерево `Plan.notes[*].subpoints[*]`
в плоский список единиц, исключая уже обработанные (`already_done_subpoint_ids`,
для resume).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник дерева заметок/подпунктов. |
| `already_done_subpoint_ids` | `set[str]` | `subpoint_id`, уже раскрытые в предыдущих сессиях. |

**Возвращаемое значение:** `list[ElaborationUnit]` — в порядке `plan.notes`, внутри заметки — в порядке `note.subpoints`.

**Исключения:** не поднимает.

### 1.3. `elaborate_outline(plan, client, status, already_done_subpoint_ids, on_batch_done, max_subpoints_per_batch) -> None`

**Описание.** Главная функция роли. Строит единицы (`build_elaboration_units`),
делит их на батчи ДВУМЯ независимыми ограничениями одновременно
(`llm/chunking.py::batch_for_quality_and_budget`):
1. токен-бюджет клиента (как в `extractor_critic`);
2. качественный потолок `max_subpoints_per_batch` — НЕ про бюджет токенов, а
   про то, что при большом числе подпунктов в одном вызове модель даёт
   поверхностные однострочные ответы (см. `llm/chunking.py` докстринг).

На каждый батч вызывает `_elaborate_batch(...)` и передаёт результат в `on_batch_done`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник дерева заметок/подпунктов. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.extraction_client`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `already_done_subpoint_ids` | `set[str]` | Для resume. |
| `on_batch_done` | `Callable[[list[str], list[Evidence]], None]` | Тот же контракт, что у `extract_evidence_from_sources` — вызывающий код (Orchestrator) ОБЯЗАН немедленно персистить `subpoint_ids` в чекпоинт (см. `orchestrator/state_machine.py::run()`, шаг "Elaborating", локальная функция `_on_batch_done`). |
| `max_subpoints_per_batch` | `int` | Качественный потолок подпунктов на один вызов. Из `settings.max_subpoints_per_generation_batch` (дефолт `6`). |

**Возвращаемое значение:** `None`. Если `units` пуст — выход немедленно, без LLM-вызовов.

**Исключения:** не перехватывает — любая ошибка `client.generate_structured(...)` всплывает наружу (в отличие от `extractor_critic.py`, здесь нет автоматической бисекции батча при `GroqSchemaError`/`GroqPromptTooLargeError` — только токен-бюджетное и качественное деление ДО вызова).

### 1.4. `_elaborate_batch(units: list[ElaborationUnit], plan: Plan, client: LLMClient, status: TaskStatus) -> list[Evidence]` (приватная)

**Описание.** Один LLM-вызов на один батч подпунктов (`role="elaborator"`).
Строит нумерованный листинг (`=== Раздел [i]: {note.title} :: {subpoint.heading} ===\n{subpoint.covers}`),
парсит `ElaborationOutput`, резолвит `item.unit_index` → `(note, subpoint)`
конкретной единицы батча (при индексе вне диапазона — приписывает факт первому
подпункту батча, с предупреждением в лог).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `units` | `list[ElaborationUnit]` | Единицы текущего батча. |
| `plan` | `Plan` | Для `plan.topic_title` в промпте. |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `list[Evidence]` — пусто, если `units` пуст. Каждый
`Evidence` получает `note_id`/`subpoint_id` соответствующей единицы,
`source_id=MODEL_KNOWLEDGE_SOURCE_ID`, `statement`/`confidence`/`is_definition`/`critic_note`
из ответа модели.

**Исключения:** пробрасывает всё, что поднимет `client.generate_structured(...)`.

**Тег роли для GroqClient:** `role="elaborator"`.

### 1.5. `elaborate_outline_sync(plan: Plan, client: LLMClient, status: TaskStatus, max_subpoints_per_batch: int = 6) -> list[Evidence]`

**Описание.** Обёртка без чекпоинтинга — для тестов/прямых вызовов вне
Orchestrator. Собирает результаты всех батчей в один список через локальный
callback `_collect` и возвращает целиком.

**Параметры:** те же, что у `elaborate_outline`, минус `on_batch_done`/`already_done_subpoint_ids` (внутри используются `set()` и локальный сборщик).

**Возвращаемое значение:** `list[Evidence]` — весь накопленный evidence по плану.

**Исключения:** те же, что у `elaborate_outline`.

---

## 2. `roles/vault_analyst.py` — роль Vault Analyst

**Назначение.** Для каждой заметки плана решает: создавать НОВУЮ заметку
(`action="create"`) или ДОПОЛНИТЬ существующую (`action="update"`,
`existing_path=...`). Основная работа — ЧИСТЫЙ КОД (локальный BM25/embedding
retrieval, `retrieval/search.py::VaultSearcher`), LLM вызывается ТОЛЬКО для
"серой зоны" схожести (`classify_similarity` вернул `"ambiguous"`) и отдельно
для распределения НОВЫХ заметок по папкам.

### 2.1. `resolve_notes_against_vault(plan, searcher, client, status, existing_folders, default_folder, high_threshold, low_threshold) -> None`

**Описание.** Мутирует `plan.notes` НА МЕСТЕ (не возвращает новый объект —
`note.action`/`note.existing_path`/`note.folder` проставляются прямо в
переданные объекты `OutlineNote`). Для каждой заметки:
1. строит поисковый запрос (`note.title + " " + все subpoint.heading`);
2. ищет `top_k=1` через `searcher.search(...)`;
3. если хитов нет — `action="create"`, добавляется в список "нуждающихся в папке";
4. иначе классифицирует `hits[0].combined_score` через `tools/dedup.py::classify_similarity(score, high_threshold, low_threshold)`:
   - `"distinct"` → `action="create"`;
   - `"duplicate"` → `action="update"`, `existing_path=hits[0].path` (БЕЗ LLM — код уверен, что это та же концепция);
   - `"ambiguous"` → один маленький LLM-вызов через `_resolve_ambiguous(...)`; при решении `"reuse"`/`"extend"` → `action="update"`; при `"distinct"` → `action="create"` (и заметка идёт в список на распределение по папкам).

После прохода по всем заметкам — если список "нуждающихся в папке" непуст,
вызывает `_assign_folders_batch(...)` ОДНИМ (или несколькими при большом
числе заметок) пакетным вызовом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Мутируется на месте — `note.action`/`existing_path`/`folder` для каждой заметки. |
| `searcher` | `VaultSearcher` (`retrieval/search.py`) | Локальный поиск по индексу Vault (BM25 + опционально embeddings). |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `existing_folders` | `list[str]` | Список папок, уже встречающихся в индексе Vault (`VaultDB.get_distinct_folders()`) — передаётся LLM как допустимые варианты для новых заметок. |
| `default_folder` | `str` | Папка по умолчанию для темы задачи (обычно `f"{settings.default_notes_folder}/{slugify(plan.topic_title)}"`, формируется в `Orchestrator.run()`). |
| `high_threshold` | `float` | Порог "точно дубликат" для `classify_similarity`. Из `settings.dedup_high_threshold` (дефолт `0.85`). |
| `low_threshold` | `float` | Порог "точно разные" для `classify_similarity`. Из `settings.dedup_low_threshold` (дефолт `0.55`). |

**Возвращаемое значение:** `None` (побочный эффект — мутация `plan.notes`).

**Исключения:** те же, что у `client.generate_structured(...)` внутри `_resolve_ambiguous`/`_assign_folders_batch` — не перехватываются.

### 2.2. `_assign_folders_batch(notes, existing_folders, default_folder, client, status) -> None` (приватная)

**Описание.** Один (иногда несколько, при большом числе новых заметок —
батчинг через `llm/chunking.py::split_items_into_batches`) пакетный
LLM-вызов на ВСЕ заметки, нуждающиеся в папке, разом — НЕ один вызов на
заметку. Системная инструкция: `llm/prompts/vault_analyst.py::FOLDER_SYSTEM_INSTRUCTION`
(указать индекс и папку — либо точное имя существующей, либо `default_folder`,
никогда не придумывать новые папки). После ответа модели — safety net:
если модель пропустила заметку в ответе ИЛИ вернула папку не из допустимого
множества (`valid_folders = set(existing_folders) | {default_folder}`) —
такой заметке принудительно проставляется `default_folder`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `notes` | `list[OutlineNote]` | Заметки, которым нужно назначить папку (мутируются на месте — `n.folder = ...`). |
| `existing_folders` | `list[str]` | См. выше. |
| `default_folder` | `str` | См. выше. |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `None` (мутация `notes` на месте).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для GroqClient:** `role="folder_assignment"`.

### 2.3. `_resolve_ambiguous(note: OutlineNote, hit: RetrievalHit, client: LLMClient, status: TaskStatus) -> str` (приватная)

**Описание.** Один маленький LLM-вызов "это та же концепция?" — только для
единственной пары (заметка плана vs. один найденный кандидат Vault) в "серой
зоне" схожести. Системная инструкция: `llm/prompts/vault_analyst.py::SYSTEM_INSTRUCTION`
(главное правило: не плодить дубликаты — при сомнении между `reuse` и
`distinct` выбирать `extend`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана (заголовок + разделы попадают в промпт). |
| `hit` | `RetrievalHit` (`retrieval/search.py`) | Найденная существующая заметка (`title`, `tags`, `summary`). |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `str` — `output.decision`, одно из `"reuse"` / `"extend"` / `"distinct"` (см. `llm/schemas.py::DedupDecisionOutput.decision`, `Literal`).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для GroqClient:** `role="vault_dedup"`.

---

## 3. `roles/synthesizer_writer.py` — роль Obsidian Writer

**Назначение.** Пишет текст ОДНОЙ заметки (Markdown-тело, теги, исходящие
wikilinks) по уже утверждённому плану (заголовок/action/папка — зафиксированы
`Plan`/`Vault Analyst`, Writer их НЕ выбирает). Вызывается один раз на КАЖДУЮ
заметку плана, из `roles/critic.py::run_critic_cycle` (см. §4).

### 3.1. `prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]`

**Описание.** ЧИСТЫЙ КОД. Строит (1) полный отсортированный список ЗАГОЛОВКОВ
всех заметок плана (даже ещё не написанных — их заголовки уже зафиксированы
планом) и (2) карту "нормализованный заголовок → каноническое написание" (для
снаппинга ссылок модели к точному написанию с учётом Unicode-вариантов дефиса,
см. `tools/markdown_tools.py::normalize_link_title`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan_notes` | `list[OutlineNote]` | Все заметки плана (`Plan.notes`). |

**Возвращаемое значение:** `tuple[list[str], dict[str, str]]` —
`(known_titles отсортированный без дублей, title_map: normalize(title) -> title)`.

**Исключения:** не поднимает.

**Примечание:** список содержит заголовки заметок ИЗ ЭТОГО ПЛАНА — заголовки
существующих заметок Vault (для ссылок на них) в текущей реализации сюда НЕ
подмешиваются этой функцией (несмотря на комментарий в докстринге про "+
существующих в Vault") — это ответственность вызывающего кода, если потребуется.

### 3.2. `write_note(note, evidence, known_titles, title_map, client, status, extra_instructions="", mark_source=None, system_instruction=None) -> DraftNote`

**Описание.** Главная функция роли. Фильтрует `evidence` только по
`e.note_id == note.note_id` (факты ДРУГИХ заметок НЕ попадают в промпт),
группирует по `subpoint_id`, строит листинг разделов с фактами (с указанием
`confidence` и, если есть, `critic_note` в виде "ПРОТИВОРЕЧИВО: ..."), строит
листинг известных заголовков для ссылок, при повторном вызове (после критика)
добавляет блок "ЗАМЕЧАНИЯ ПО ПРЕДЫДУЩЕЙ ВЕРСИИ". Делает один
`generate_structured(role="synthesizer_write", ...)`, затем конвертирует ответ
в `DraftNote` через `_to_draft_note(...)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана — источник `title`/`action`/`folder`/`existing_path`/`subpoints`. |
| `evidence` | `list[Evidence]` | ВЕСЬ evidence задачи (не только этой заметки) — фильтрация по `note_id` происходит ВНУТРИ функции. |
| `known_titles` | `list[str]` | Из `prepare_linking_context(...)`. |
| `title_map` | `dict[str, str]` | Из `prepare_linking_context(...)` — для снаппинга ссылок в `_to_draft_note`. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `extra_instructions` | `str` (дефолт `""`) | Feedback критика при повторном вызове (см. `roles/critic.py::run_critic_cycle`) — вставляется в промпт как отдельный блок. |
| `mark_source` | `str \| None` (дефолт `None`) | Если задано (напр. `"model-knowledge"`) — прокидывается в `frontmatter["source"]` итогового `DraftNote`. |
| `system_instruction` | `str \| None` (дефолт `None`) | Позволяет вызывающему коду (Orchestrator) подставить нестандартный системный промпт (напр. с `MERGE_AWARENESS_GUIDANCE`, см. `llm/prompts/synthesizer_writer.py`); при `None` используется дефолтный `WRITE_SYSTEM_INSTRUCTION`. |

**Возвращаемое значение:** `DraftNote` (`storage/models.py`) — см. `_to_draft_note` ниже за детали построения.

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`; отдельно —
см. `_to_draft_note` (`ValueError` при `action="update"` без `existing_path`).

**Тег роли для GroqClient:** `role="synthesizer_write"`.

### 3.3. `_to_draft_note(note: OutlineNote, output: DraftNoteOutput, title_map: dict[str, str], mark_source: str | None = None) -> DraftNote` (приватная)

**Описание.** ЧИСТЫЙ КОД — конвертирует сырой ответ модели (`DraftNoteOutput`)
в доменный `DraftNote`, с ключевым архитектурным правилом: **путь, action,
папка и заголовок решает ПЛАН (Vault Analyst), а НЕ то, что вернула модель на
этом шаге** — даже если `output.action` не совпадает с `note.action`, он
игнорируется (см. тест `test_write_note_update_uses_existing_path_from_plan_not_from_model`).

Логика:
- если `note.action == "update"` — путь берётся из `note.existing_path`
  (обязателен, иначе `ValueError`);
- иначе — путь строится через `tools/markdown_tools.py::build_note_path(note.folder, note.title)`;
- `frontmatter = {"created": сегодняшняя дата UTC}`, плюс `frontmatter["source"] = mark_source`, если задан;
- прочие ключи `output.frontmatter_extra` от модели ИГНОРИРУЮТСЯ намеренно
  (состав frontmatter — единая точка правды в коде, не в промпте, см.
  `tools/markdown_tools.py::_ALLOWED_FRONTMATTER_KEYS`);
- каждая ссылка из `output.links_out` прогоняется через внутреннюю
  `_resolve_link(raw)`: снимает обрамляющие `[[...]]` (`strip_wikilink_brackets`),
  отбрасывает URL-подобные значения (по ошибке модели, с предупреждением в лог),
  снаппит к канонической форме через `title_map.get(normalize_link_title(link), link)`
  (если заголовка нет в карте — оставляет как есть, т.е. допускает ссылку на
  заголовок вне списка).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Источник истины по `action`/`path`/`folder`/`title`/`note_id`. |
| `output` | `DraftNoteOutput` | Сырой ответ модели (`llm/schemas.py`) — источник `body_md`/`tags`/`links_out`/`append_section`. |
| `title_map` | `dict[str, str]` | Для снаппинга ссылок. |
| `mark_source` | `str \| None` | См. `write_note`. |

**Возвращаемое значение:** `DraftNote` с полями `note_id`, `action`, `path`,
`title`, `folder`, `frontmatter`, `body_md` (через `sanitize_wikilinks`),
`tags`, `links_out` (только успешно резолвленные), `append_section` (через
`sanitize_wikilinks`, `None` если пусто).

**Исключения:** `ValueError` — если `note.action == "update"` и `note.existing_path` пуст.

### 3.4. `build_relationships(drafts: list[DraftNote]) -> list[Relationship]`

**Описание.** ЧИСТЫЙ КОД, без LLM. Строит карту "заголовок → путь" по уже
написанным черновикам, затем для каждой исходящей ссылки каждого черновика
создаёт `Relationship(from_note=path, to_note=..., link_type="wikilink")`.
Если целевой заголовок не найден в карте (ссылка на заметку, не входящую в
этот batch черновиков — например, на существующую заметку Vault или красную
ссылку) — `to_note` остаётся самим заголовком as-is (fallback).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Все черновики задачи (и create, и update). |

**Возвращаемое значение:** `list[Relationship]` — по одной записи на каждую пару (черновик, исходящая ссылка).

**Исключения:** не поднимает.

---

## 4. `roles/critic.py` — роль Critic (bounded-retry ревью)

**Назначение.** Работает ПОСЛЕ Writer, на уже НАПИСАННОМ тексте заметки (а не
на сыром evidence, в отличие от Critic внутри `extractor_critic.py`, который
сверял утверждения с текстом источника). Проверяет ВНУТРЕННЮЮ согласованность
и полноту относительно уже собранного evidence — фактическую точность против
внешнего мира проверить нечем (см. `llm/prompts/critic.py` докстринг). Реализует
bounded retry: НЕ цикл "пока не одобрит", а жёсткий потолок `max_rounds`
переписываний.

### 4.1. `review_draft(draft: DraftNote, assigned_evidence: list[Evidence], client: LLMClient, status: TaskStatus) -> CriticVerdictOutput`

**Описание.** Один вызов ревью. Берёт текст заметки (`draft.append_section or draft.body_md`
— т.е. для update-заметок ревьюется именно добавляемый блок, не весь файл),
строит листинг фактов, которые должны быть отражены (или заглушку "факты не
были назначены явно", если `assigned_evidence` пуст), делает
`generate_structured(role="critic", ...)` с системной инструкцией
`llm/prompts/critic.py::SYSTEM_INSTRUCTION` (проверяет: внутреннюю
согласованность; полноту относительно списка фактов; структуру/ясность
изложения; отсутствие придуманных точных деталей сверх переданных фактов;
полноту покрытия заголовков плана).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `draft` | `DraftNote` | Уже написанная заметка (результат `synthesizer_writer.write_note`). |
| `assigned_evidence` | `list[Evidence]` | Факты, назначенные ИМЕННО этой заметке (фильтрация по `note_id` делается ВНЕ этой функции, в `run_critic_cycle`, см. ниже). |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `CriticVerdictOutput` (`llm/schemas.py`) — `verdict: Literal["ok", "rewrite"]`, `feedback: str` (заполнен только при `"rewrite"`).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для GroqClient:** `role="critic"`.

### 4.2. `run_critic_cycle(note, evidence, known_titles, title_map, client, status, max_rounds, mark_source=None, system_instruction=None) -> DraftNote`

**Описание.** Оркестрирует цикл "Writer → Critic → (при rewrite) Writer снова"
для ОДНОЙ заметки, СТРОГО ОГРАНИЧЕННЫЙ `max_rounds` переписываний:

1. Пишет первую версию: `synthesizer_writer.write_note(note, evidence, known_titles, title_map, client, status, mark_source=mark_source, system_instruction=system_instruction)`.
2. Если `max_rounds <= 0` — критик вообще не вызывается, возвращает первую версию как есть (эквивалент "критик выключен").
3. Иначе фильтрует `assigned_evidence = [e for e in evidence if e.note_id == note.note_id]` (важно: только здесь, а не в `review_draft`).
4. Цикл `while rounds < max_rounds`: вызывает `review_draft(...)`; если `verdict == "ok"` — `break`; иначе `rounds += 1` и заметка переписывается заново через `write_note(..., extra_instructions=verdict.feedback, ...)`.
5. После выхода из цикла — `draft.critic_rounds = rounds`, `draft.needs_review = (rounds >= max_rounds and последний verdict == "rewrite")`.

**Ключевое architectural-решение** (см. `docs/architecture.md §2.3`): если
бюджет раундов исчерпан, а вердикт всё ещё `"rewrite"` — заметка уходит в
staging КАК ЕСТЬ (последняя переписанная версия), С ПОМЕТКОЙ `needs_review=True`,
БЕЗ дополнительного финального ре-ревью (сознательная экономия одного вызова
критика — иначе цикл не был бы по-настоящему ограниченным).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана. |
| `evidence` | `list[Evidence]` | Весь evidence задачи (фильтрация внутри). |
| `known_titles` | `list[str]` | См. `prepare_linking_context`. |
| `title_map` | `dict[str, str]` | См. `prepare_linking_context`. |
| `client` | `LLMClient` | LLM-клиент (один и тот же для Writer и Critic — оба вызова идут через один `client`). |
| `status` | `TaskStatus` | Учёт бюджета. |
| `max_rounds` | `int` | Жёсткий потолок переписываний. Из `settings.max_critic_rounds` (дефолт `1`). `0` — критик полностью выключен. |
| `mark_source` | `str \| None` (дефолт `None`) | Прокидывается во ВСЕ вызовы `write_note` (включая переписывания). |
| `system_instruction` | `str \| None` (дефолт `None`) | Прокидывается во ВСЕ вызовы `write_note`. |

**Возвращаемое значение:** `DraftNote` — финальная (возможно, переписанная)
версия, с дополнительно проставленными `critic_rounds` и `needs_review`.

**Исключения:** пробрасывает ошибки `client.generate_structured(...)` из
`write_note`/`review_draft` (в т.ч. `LLMTaskBudgetExceeded`/`LLMFreeLimitReached`
— если бюджет кончился посреди цикла критика, заметка НЕ будет достроена в
рамках этой сессии; `Orchestrator` в этом случае ловит исключение на уровне
всей задачи, см. документацию цикла Часть 1, и заметка не попадёт в
`checkpoint.drafts`, т.к. `persist("synthesizing")` вызывается только ПОСЛЕ
успешного завершения `run_critic_cycle` для заметки).

---

## Сводная таблица: роль → тег для GroqClient → клиент Orchestrator'а

| Файл / функция | `role=` | Клиент | Активность |
|---|---|---|---|
| `outline_planner.build_plan` | `"outline_planner"` | `self.llm` | Всегда |
| `researcher.select_relevant_sources` | `"researcher_selection"` | `self.llm` | Только `RESEARCH_MODE=web` (сейчас не вызывается Orchestrator'ом) |
| `extractor_critic._extract_from_batch` | `"extractor_critic"` | `self.extraction_client` | Только `RESEARCH_MODE=web` (сейчас не вызывается) |
| `elaborator._elaborate_batch` | `"elaborator"` | `self.extraction_client` | `RESEARCH_MODE=knowledge` (дефолт) |
| `vault_analyst._resolve_ambiguous` | `"vault_dedup"` | `self.llm` | Всегда, только для "серой зоны" |
| `vault_analyst._assign_folders_batch` | `"folder_assignment"` | `self.llm` | Всегда, только для НОВЫХ заметок |
| `synthesizer_writer.write_note` | `"synthesizer_write"` | `self.llm` | Всегда, раз (и более) на заметку |
| `critic.review_draft` | `"critic"` | `self.llm` | Всегда, если `max_critic_rounds > 0` |

Документация по `roles/` завершена.
