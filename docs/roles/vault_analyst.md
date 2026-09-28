# Документация: `roles/vault_analyst.py` — роль Vault Analyst

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Для каждой заметки плана решает: создавать НОВУЮ заметку
(`action="create"`) или ДОПОЛНИТЬ существующую (`action="update"`,
`existing_path=...`). Основная работа — ЧИСТЫЙ КОД (локальный
BM25/embedding retrieval, `retrieval/search.py::VaultSearcher` — сам модуль
`retrieval/` пока без отдельного reference-дока, см. `../README.md`), LLM
вызывается ТОЛЬКО для "серой зоны" схожести (`classify_similarity` вернул
`"ambiguous"`) и отдельно для распределения НОВЫХ заметок по папкам.

## 1. `resolve_notes_against_vault(plan, searcher, client, status, existing_folders, default_folder, high_threshold, low_threshold) -> None`

**Описание.** Мутирует `plan.notes` НА МЕСТЕ (не возвращает новый объект —
`note.action`/`note.existing_path`/`note.folder` проставляются прямо в
переданные объекты `OutlineNote`, `../storage/models.md §1.3`). Для каждой
заметки:
1. строит поисковый запрос (`note.title + " " + все subpoint.heading`);
2. ищет `top_k=1` через `searcher.search(...)`;
3. если хитов нет — `action="create"`, добавляется в список "нуждающихся
   в папке";
4. иначе классифицирует `hits[0].combined_score` через
   `tools/dedup.py::classify_similarity(score, high_threshold, low_threshold)`
   (`../tools/dedup.md`):
   - `"distinct"` → `action="create"`;
   - `"duplicate"` → `action="update"`, `existing_path=hits[0].path` (БЕЗ
     LLM — код уверен, что это та же концепция);
   - `"ambiguous"` → один маленький LLM-вызов через `_resolve_ambiguous(...)`
     (§3); при решении `"reuse"`/`"extend"` → `action="update"`; при
     `"distinct"` → `action="create"` (и заметка идёт в список на
     распределение по папкам).

После прохода по всем заметкам — если список "нуждающихся в папке" непуст,
вызывает `_assign_folders_batch(...)` (§2) ОДНИМ (или несколькими при
большом числе заметок) пакетным вызовом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Мутируется на месте — `note.action`/`existing_path`/`folder` для каждой заметки. |
| `searcher` | `VaultSearcher` (`retrieval/search.py`) | Локальный поиск по индексу Vault (BM25 + опционально embeddings). |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `existing_folders` | `list[str]` | Список папок, уже встречающихся в индексе Vault (`vault/db.py::get_distinct_folders`, `../vault/db.md`) — передаётся LLM как допустимые варианты для новых заметок. |
| `default_folder` | `str` | Папка по умолчанию для темы задачи (обычно `f"{settings.default_notes_folder}/{slugify(plan.topic_title)}"`, `../config/settings.md §6`). |
| `high_threshold` | `float` | Порог "точно дубликат" (`../config/settings.md §8`, дефолт `0.85`). |
| `low_threshold` | `float` | Порог "точно разные" (дефолт `0.55`). |

**Возвращаемое значение:** `None` (побочный эффект — мутация `plan.notes`).

**Исключения:** те же, что у `client.generate_structured(...)` внутри
`_resolve_ambiguous`/`_assign_folders_batch` — не перехватываются.

## 2. `_assign_folders_batch(notes, existing_folders, default_folder, client, status) -> None` (приватная)

**Описание.** Один (иногда несколько, при большом числе новых заметок —
батчинг через `llm/chunking.py::split_items_into_batches`,
`../llm/chunking.md §1`) пакетный LLM-вызов на ВСЕ заметки, нуждающиеся в
папке, разом — НЕ один вызов на заметку. Системная инструкция:
`llm/prompts/vault_analyst.py::FOLDER_SYSTEM_INSTRUCTION`
(`../llm/prompts.md §4`: указать индекс и папку — либо точное имя
существующей, либо `default_folder`, никогда не придумывать новые папки).
После ответа модели — safety net: если модель пропустила заметку в ответе
ИЛИ вернула папку не из допустимого множества (`valid_folders =
set(existing_folders) | {default_folder}`) — такой заметке принудительно
проставляется `default_folder`.

**Возвращаемое значение:** `None` (мутация `notes` на месте).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для `GroqClient`:** `role="folder_assignment"`.

## 3. `_resolve_ambiguous(note: OutlineNote, hit: RetrievalHit, client: LLMClient, status: TaskStatus) -> str` (приватная)

**Описание.** Один маленький LLM-вызов "это та же концепция?" — только для
единственной пары (заметка плана vs. один найденный кандидат Vault) в
"серой зоне" схожести. Системная инструкция:
`llm/prompts/vault_analyst.py::SYSTEM_INSTRUCTION` (`../llm/prompts.md §4`:
главное правило — не плодить дубликаты, при сомнении между `reuse` и
`distinct` выбирать `extend`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана (заголовок + разделы попадают в промпт). |
| `hit` | `RetrievalHit` (`retrieval/search.py`) | Найденная существующая заметка (`title`, `tags`, `summary`). |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `str` — `output.decision`, одно из `"reuse"` /
`"extend"` / `"distinct"` (`../llm/schemas.md §3`, `DedupDecisionOutput.decision`).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для `GroqClient`:** `role="vault_dedup"`.

Документация по `roles/vault_analyst.py` завершена. Обзор пакета —
`_index.md`. Следующий шаг пайплайна — `synthesizer_writer.md` /
`critic.md`.
