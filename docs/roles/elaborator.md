# Документация: `roles/elaborator.py` — роль Elaborator

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Эквивалент Extractor+Critic для `RESEARCH_MODE=knowledge`.
В отличие от Extractor, здесь НЕТ входного текста источника — модель
раскрывает каждый подпункт плана (`OutlineSubpoint`) из СОБСТВЕННЫХ знаний.
Результат — тот же тип объекта `Evidence` (`../storage/models.md §2`), что
и у Extractor+Critic — это то, что позволяет `vault_analyst`,
`synthesizer_writer` не знать, в каком режиме работает система. Это САМЫЙ
ЧАСТЫЙ по числу вызовов шаг в knowledge-режиме — использует отдельный
клиент `Orchestrator.self.extraction_client`
(`../orchestrator/state_machine.md §4`).

### `MODEL_KNOWLEDGE_SOURCE_ID: str = "model_knowledge"`

Константа — значение `Evidence.source_id` для ВСЕХ фактов, произведённых
этой ролью (т.к. у них нет реального внешнего источника). Совпадает по
смыслу (но не по написанию) с
`orchestrator/state_machine.py::KNOWLEDGE_MODE_FRONTMATTER_SOURCE =
"model-knowledge"` (`../orchestrator/state_machine.md §1`) — первая
маркирует `Evidence` (внутренний объект), вторая — YAML frontmatter
итоговой заметки (видимое пользователю значение).

## 1. `class ElaborationUnit` (dataclass)

Единица батчинга — ОДИН подпункт ОДНОЙ заметки плана.

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана, к которой принадлежит подпункт. |
| `subpoint` | `OutlineSubpoint` | Сам подпункт (`heading`, `covers`, `subpoint_id`). |

## 2. `build_elaboration_units(plan: Plan, already_done_subpoint_ids: set[str]) -> list[ElaborationUnit]`

**Описание.** ЧИСТЫЙ КОД, без LLM. "Разворачивает" дерево
`Plan.notes[*].subpoints[*]` в плоский список единиц, исключая уже
обработанные (`already_done_subpoint_ids`, для resume).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник дерева заметок/подпунктов. |
| `already_done_subpoint_ids` | `set[str]` | `subpoint_id`, уже раскрытые в предыдущих сессиях. |

**Возвращаемое значение:** `list[ElaborationUnit]` — в порядке `plan.notes`,
внутри заметки — в порядке `note.subpoints`.

**Исключения:** не поднимает.

## 3. `elaborate_outline(plan, client, status, already_done_subpoint_ids, on_batch_done, max_subpoints_per_batch) -> None`

**Описание.** Главная функция роли. Строит единицы (§2), делит их на батчи
ДВУМЯ независимыми ограничениями одновременно
(`llm/chunking.py::batch_for_quality_and_budget`, `../llm/chunking.md §2`):
1. токен-бюджет клиента;
2. качественный потолок `max_subpoints_per_batch` — НЕ про бюджет, а про
   то, что при большом числе подпунктов в одном вызове модель даёт
   поверхностные однострочные ответы.

На каждый батч вызывает `_elaborate_batch(...)` (§4) и передаёт результат в
`on_batch_done`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник дерева заметок/подпунктов. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.extraction_client`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `already_done_subpoint_ids` | `set[str]` | Для resume. |
| `on_batch_done` | `Callable[[list[str], list[Evidence]], None]` | Вызывающий код (`Orchestrator`) ОБЯЗАН немедленно персистить `subpoint_ids` в чекпоинт (`../staging/checkpoint.md`). |
| `max_subpoints_per_batch` | `int` | Качественный потолок подпунктов на один вызов. Из `settings.max_subpoints_per_generation_batch` (дефолт `6`, `../config/settings.md §9`). |

**Возвращаемое значение:** `None`. Если `units` пуст — выход немедленно,
без LLM-вызовов.

**Исключения:** не перехватывает — любая ошибка `client.generate_structured(...)`
всплывает наружу (в отличие от web-режима, здесь нет автоматической
бисекции батча при `GroqSchemaError`/`GroqPromptTooLargeError` — только
токен-бюджетное и качественное деление ДО вызова).

## 4. `_elaborate_batch(units, plan, client, status) -> list[Evidence]` (приватная)

**Описание.** Один LLM-вызов на один батч подпунктов (`role="elaborator"`).
Строит нумерованный листинг (`=== Раздел [i]: {note.title} :: {subpoint.heading} ===\n{subpoint.covers}`),
парсит `ElaborationOutput` (`../llm/schemas.md §2`), резолвит
`item.unit_index` → `(note, subpoint)` конкретной единицы батча (при
индексе вне диапазона — приписывает факт первому подпункту батча, с
предупреждением в лог).

**Возвращаемое значение:** `list[Evidence]` — пусто, если `units` пуст.
Каждый `Evidence` получает `note_id`/`subpoint_id` соответствующей
единицы, `source_id=MODEL_KNOWLEDGE_SOURCE_ID`,
`statement`/`confidence`/`is_definition`/`critic_note` из ответа модели.

**Исключения:** пробрасывает всё, что поднимет `client.generate_structured(...)`.

**Тег роли для `GroqClient`:** `role="elaborator"`.

## 5. `elaborate_outline_sync(plan: Plan, client: LLMClient, status: TaskStatus, max_subpoints_per_batch: int = 6) -> list[Evidence]`

**Описание.** Обёртка без чекпоинтинга — для тестов/прямых вызовов вне
`Orchestrator`. Собирает результаты всех батчей в один список через
локальный callback `_collect` и возвращает целиком.

**Параметры:** те же, что у `elaborate_outline`, минус
`on_batch_done`/`already_done_subpoint_ids` (внутри используются `set()` и
локальный сборщик).

**Возвращаемое значение:** `list[Evidence]` — весь накопленный evidence по
плану.

**Исключения:** те же, что у `elaborate_outline`.

Документация по `roles/elaborator.py` завершена. Обзор пакета —
`_index.md`. Следующий шаг пайплайна — `vault_analyst.md`.
