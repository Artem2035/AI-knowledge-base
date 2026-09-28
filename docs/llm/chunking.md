# Документация: `llm/chunking.py` — батчинг элементов под токен-бюджет

> Reference-док. Обзор пакета — `_index.md`. Схемы, под которые батчатся
> элементы — `schemas.md`. Кто вызывает эти функции — конкретные роли,
> `../roles/`.

**Назначение (из докстринга модуля).** Общая утилита для деления списка
элементов на батчи под доступный prompt-бюджет клиента. Нужна, чтобы
избежать silent auto-truncate (`GroqClient._auto_truncate_prompt` режет
промпт С КОНЦА — для списков кандидатов это означает потерю "хвостовых"
элементов до того, как их вообще увидела модель, см. `groq_client.md §6.6`).
Для клиентов БЕЗ `available_prompt_budget_tokens()` деление не нужно —
возвращается один батч со всеми элементами.

## 1. `split_items_into_batches(items, *, client, system_instruction, response_model, render_item, static_overhead_text="") -> list[list[T]]`

**Описание.** Универсальный батчер по ЧИСТО ТОКЕННОМУ бюджету:
1. Если `items` пуст — возвращает `[]` немедленно.
2. `budget_fn = getattr(client, "available_prompt_budget_tokens", None)` —
   если у клиента НЕТ этого метода — возвращает ОДИН батч со всеми
   элементами, без деления.
3. `available_tokens = budget_fn(system_instruction, response_model)` —
   если `None` — также один батч без деления.
4. `overhead_tokens = estimate_tokens(static_overhead_text)`,
   `text_budget_tokens = max(available_tokens - overhead_tokens, 0)`.
5. Если `text_budget_tokens <= 0` — КАЖДЫЙ элемент идёт ОТДЕЛЬНЫМ батчем,
   дальше клиент сам решит (auto-truncate/ошибка), но хотя бы НЕ теряется
   весь список сразу.
6. Иначе — жадный проход по `items`: накапливает `current` батч, суммируя
   `estimate_tokens(render_item(item))`; если добавление ОЧЕРЕДНОГО
   элемента превысило бы `text_budget_tokens` (и текущий батч уже НЕПУСТ) —
   батч закрывается, начинается новый.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `items` | `Sequence[T]` | Элементы для батчинга. |
| `client` | LLM-клиент (keyword-only) | Источник `available_prompt_budget_tokens(...)`. |
| `system_instruction` | `str` (keyword-only) | Для оценки накладных расходов схемы+системы. |
| `response_model` | `type[BaseModel]` (keyword-only) | Класс ожидаемого ответа — его JSON Schema тоже занимает токены. |
| `render_item` | `Callable[[T], str]` (keyword-only) | Как отрендерить ОДИН элемент в текст для оценки размера в токенах. |
| `static_overhead_text` | `str` (keyword-only, дефолт `""`) | Дополнительный статичный текст промпта (не зависящий от батча). |

**Возвращаемое значение:** `list[list[T]]` — список батчей, сохраняющих
исходный порядок элементов.

**Исключения:** не поднимает — деградирует до "один элемент = один батч" в
худшем случае, а не падает.

**Кто вызывает:** `roles/vault_analyst.py::_assign_folders_batch`
(`../roles/vault_analyst.md`), а также §2 ниже (внутри
`batch_for_quality_and_budget`).

## 2. `batch_for_quality_and_budget(items, *, client, system_instruction, response_model, render_item, static_overhead_text, max_items_per_batch) -> list[list[T]]`

**Описание.** Комбинирует ДВА НЕЗАВИСИМЫХ предела на размер батча:
1. токен-бюджет (§1);
2. КАЧЕСТВЕННЫЙ потолок `max_items_per_batch` — НЕ про бюджет, а про то,
   что при БОЛЬШОМ числе элементов в одном вызове модель скатывается в
   однострочные поверхностные ответы на каждый (напр. заметка с 30
   подпунктами в одном вызове).

Реализация: сначала `split_items_into_batches` делит по токен-бюджету, затем
КАЖДЫЙ полученный батч дополнительно нарезается
`range(0, len(batch), max_items_per_batch)`.

Порядок элементов сохраняется, поэтому границы батчей ЧАЩЕ ВСЕГО совпадают с
границами заметок. Исключение: если шаг (1) уже разрезал ПОСЕРЕДИНЕ заметки
по бюджету (при длинных элементах) — тогда шаг (2) режет уже этот кусок, и
последний под-батч может оказаться короче `max_items_per_batch`. Для
коротких `heading+covers` (типичный случай) это практически не происходит.

**Параметры:** те же, что у `split_items_into_batches`, ПЛЮС:

| Имя | Тип | Назначение |
|---|---|---|
| `max_items_per_batch` | `int` (keyword-only) | Качественный потолок элементов на один вызов. Из `settings.max_subpoints_per_generation_batch` (дефолт `6`). |

**Возвращаемое значение:** `list[list[T]]` — финальные батчи, каждый не
длиннее `max_items_per_batch` (кроме описанного выше редкого исключения).

**Исключения:** те же, что у `split_items_into_batches` (не поднимает).

**Кто вызывает:** ИСКЛЮЧИТЕЛЬНО `roles/elaborator.py::elaborate_outline`
(`../roles/elaborator.md`) — единственное место в проекте, где используется
ДВОЙНОЕ ограничение (токен + качество).

---

## Сводная схема: путь одного батча от плана до промпта

```
roles/elaborator.py::elaborate_outline(plan, client, status, ...)
        │
        ▼
build_elaboration_units(plan, already_done_subpoint_ids)  ── list[ElaborationUnit]
        │
        ▼
batch_for_quality_and_budget(
    units, client=client,
    system_instruction=llm/prompts/elaborator.py::SYSTEM_INSTRUCTION,   (см. prompts.md §3)
    response_model=llm/schemas.py::ElaborationOutput,                  (см. schemas.md §2)
    render_item=lambda u: f"{u.note.title} :: {u.subpoint.heading}\n{u.subpoint.covers}",
    static_overhead_text=...,
    max_items_per_batch=settings.max_subpoints_per_generation_batch,
)
        │
        ▼  для каждого батча
_elaborate_batch(batch, plan, client, status)
        │
        ▼
client.generate_structured(
    role="elaborator",
    prompt=<нумерованный листинг батча>,
    response_model=llm/schemas.py::ElaborationOutput,
    status=status,
    system_instruction=llm/prompts/elaborator.py::SYSTEM_INSTRUCTION,
)
        │
        ▼
GroqClient.generate_structured(...)  ── см. groq_client.md §6.4
        │
        ▼
ElaborationOutput  →  список storage/models.py::Evidence (через unit_index)
```

Документация по `llm/chunking.py` завершена. Весь пакет `llm/` — см.
`_index.md`.
