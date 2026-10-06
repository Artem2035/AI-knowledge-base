# Документация: `roles/outline_planner.py` — роль Planner

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Превращает запрос пользователя в `Plan` (`../storage/models.md`): тема, **домен**, абзац-резюме и дерево `OutlineNote` → `OutlineSubpoint` (с типом раздела `kind`). Первый LLM-вызов задачи, `role="outline_planner"`, клиент `self.llm`. Резерв вывода: `groq_reserved_output_by_role["outline_planner"]` (3000), `reasoning_effort` не задан (дефолт модели).

## `build_plan(task: Task, client: LLMClient, status: TaskStatus) -> Plan`

Системная инструкция: `llm/prompts/outline_planner.py::OUTLINE_PLANNER_SYSTEM_INSTRUCTION` (`../llm/prompts.md`). Правила: заметка — самостоятельная концепция; число заметок и подпунктов определяет полнота темы; `covers` — техзадание, а не текст; логичный порядок подпунктов; классификация домена (п. 5); `kind` только из набора домена (п. 6); `summary` — абзац (4–6 предложений) для читателя, незнакомого с темой, без лишних чисел, дат и имён (п. 7). `summary` используется как `[!abstract] Кратко` в MOC.

**Нормализация в коде, а не доверие модели:**
- `domain` вне `DOMAIN_KINDS` → `DEFAULT_DOMAIN` (`technical`);
- `kind` не из набора домена → `"other"` (`normalize_kind`).

`note_id` и `subpoint_id` назначает код (`default_factory`), не модель.

| Имя | Тип | Назначение |
|---|---|---|
| `task` | `Task` | `task_id`, `raw_query` (в промпт через `!r`). |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращает:** `Plan(task_id, topic_title, summary, domain, notes=[OutlineNote(title, rationale, subpoints=[OutlineSubpoint(heading, covers, kind)])])`.

**Исключения:** всё, что поднимает `client.generate_structured(...)`; не перехватываются.

Документация по `roles/outline_planner.py` завершена. Далее — `elaborator.md`.