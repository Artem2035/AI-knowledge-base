# Документация: `roles/outline_planner.py` — роль Planner

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Единственная функция здесь превращает сырой запрос
пользователя (`Task.raw_query`) в структурированный план конспекта (`Plan`
с деревом `OutlineNote` → `OutlineSubpoint`, `../storage/models.md §1`). Это
ПЕРВЫЙ LLM-вызов задачи (`role="outline_planner"`) и самый дешёвый по
объёму промпта.

## `build_plan(task: Task, client: LLMClient, status: TaskStatus) -> Plan`

**Описание.** Строит промпт из `task.raw_query`, отправляет один
`generate_structured` вызов с системной инструкцией
`llm/prompts/outline_planner.py::OUTLINE_PLANNER_SYSTEM_INSTRUCTION`
(`../llm/prompts.md §1`: заметка = самостоятельная атомарная концепция;
число заметок и подпунктов определяется полнотой темы, а не круглым
числом; для каждого подпункта — короткое техзадание `covers`, а не сам
текст; порядок подпунктов — определение → механизм → применение →
ограничения, где уместно). Возвращает распарсенный `OutlinePlanOutput`
(`../llm/schemas.md §1`) и конвертирует его в доменный объект `Plan`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `task` | `Task` (`../storage/models.md §1.1`) | Источник `task.task_id` (переносится в `Plan.task_id`) и `task.raw_query` (подставляется в промпт как есть, в кавычках через `!r`). |
| `client` | `LLMClient` (`../llm/core.md §1`) | Обычно `Orchestrator.self.llm` (не extraction-клиент — planning дешёвый и нечастый). |
| `status` | `TaskStatus` (`../storage/models.md §6.2`) | Передаётся насквозь в `client.generate_structured(...)` для учёта бюджета. |

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
их не назначает, это ответственность кода, не модели (см.
`../GLOSSARY.md` — "foreign keys не должны придумываться LLM").

**Исключения:** любые, поднимаемые `client.generate_structured(...)`
(`LLMTaskBudgetExceeded`, `LLMFreeLimitReached` — `../orchestrator/budget.md
§1–2`; `GroqPromptTooLargeError`, `GroqSchemaError` — `../llm/groq_client.md
§1`) — эта функция их не перехватывает, они всплывают до
`Orchestrator.run()`.

**Тег роли для `GroqClient`:** `role="outline_planner"`.

Документация по `roles/outline_planner.py` завершена. Обзор пакета —
`_index.md`. Следующий шаг пайплайна — `elaborator.md`.
