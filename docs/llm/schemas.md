# Документация: `llm/schemas.py` — контракты structured-output

> Reference-док. Обзор пакета — `_index.md`. Промпты, использующие эти схемы
> — `prompts.md`. Батчинг списков элементов под эти схемы — `chunking.md`.

**Назначение (из докстринга модуля).** Намеренно ОТДЕЛЕНЫ от
`storage/models.py` (`../storage/models.md`): LLM НЕ должна сама придумывать
`task_id`/`path`/`source_id` (это foreign keys, которыми управляет код) —
вместо этого модель ссылается на ИНДЕКСЫ элементов, переданных ей в
промпте, а код-обвязка роли уже сама подставляет реальные id/пути. Это
снижает риск галлюцинаций в структурных полях. Простые типы
(`str`/`float`/`bool`/`list`) вместо произвольных `dict` — потому что JSON
Schema, которую LLM использует для structured output, работает надёжнее с
ФИКСИРОВАННОЙ формой полей.

Все классы ниже — `pydantic.BaseModel`, без собственных методов (чистые
схемы данных) — валидация стандартная Pydantic (`ValidationError` при
несоответствии типов).

## 1. Planner

### `class OutlineSubpointOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `heading` | `str` | Заголовок подпункта. |
| `covers` | `str` | Техзадание — что раскрыть, не сам текст. |

### `class OutlineNoteOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `title` | `str` | Заголовок заметки. |
| `subpoints` | `list[OutlineSubpointOutput]` | `default_factory=list`. |
| `rationale` | `str` | Дефолт `""`. Обоснование выделения заметки. |

### `class OutlinePlanOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `topic_title` | `str` | Общее название темы. |
| `summary` | `str` | Дефолт `""`. |
| `notes` | `list[OutlineNoteOutput]` | `default_factory=list`. |

**Кто использует:** `roles/outline_planner.py::build_plan` (`role="outline_planner"`,
`../roles/outline_planner.md`) — конвертируется в `storage/models.py::Plan`
(`../storage/models.md §1.4`).

## 2. Elaborator (`RESEARCH_MODE=knowledge`)

**Отличие от прежней схемы web-режима (`EvidenceItem`/`EvidenceBatchOutput`,
не документируется в этом заходе — см. `../README.md` за статусом
web-режима):** здесь НЕТ `unit_index`/`contradicts_indices` в смысле
противоречий МЕЖДУ источниками — не с чем сверять противоречия между
"единицами текста источника", т.к. текста источника нет: вход — сама
подтема, а не чанк чужого текста.

### `class ElaborationItem(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `statement` | `str` | Сам текст утверждения. |
| `confidence` | `float` | `ge=0.0, le=1.0`. |
| `is_definition` | `bool` | Дефолт `False`. |
| `critic_note` | `str` | Дефолт `""`. |
| `unit_index` | `int` | Дефолт `0`. Индекс подтемы в списке, переданном в промпте текущего батча (0-based) — позволяет раскрывать НЕСКОЛЬКО подтем одним вызовом и корректно приписать каждый факт к его настоящей подтеме в коде-обвязке (`roles/elaborator.py`, `../roles/elaborator.md`). |

### `class ElaborationOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `evidence` | `list[ElaborationItem]` | `default_factory=list`. |

**Кто использует:** `roles/elaborator.py::_elaborate_batch` (`role="elaborator"`,
`../roles/elaborator.md`).

## 3. Vault dedup (только "серая зона")

### `class DedupDecisionOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `same_concept` | `bool` | Явный флаг "это та же концепция?" (дублирует смысл `decision`, но отдельным булевым полем). |
| `decision` | `Literal["reuse", "extend", "distinct"]` | Итоговое решение — см. `roles/vault_analyst.py::_resolve_ambiguous` (`../roles/vault_analyst.md`). |
| `reasoning` | `str` | Дефолт `""`. |

**Кто использует:** `roles/vault_analyst.py::_resolve_ambiguous`
(`role="vault_dedup"`).

## 4. Synthesizer + Writer

### `class FrontmatterField(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `key` | `str` | Имя предложенного ключа frontmatter. |
| `value` | `str` | Значение. |

**Примечание:** объект существует в схеме, но
`roles/synthesizer_writer.py::_to_draft_note` НАМЕРЕННО игнорирует
`output.frontmatter_extra` целиком — состав итогового YAML frontmatter
ограничен кодом (`tools/markdown_tools.py::_ALLOWED_FRONTMATTER_KEYS`,
`../tools/markdown_tools.md`), а не тем, что предложит модель.

### `class DraftNoteOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `action` | `Literal["create", "update"]` | ИГНОРИРУЕТСЯ кодом при построении `DraftNote` — путь/action/папку/заголовок решает ПЛАН (`OutlineNote`), а не ответ модели (см. `../roles/synthesizer_writer.md`, тест `test_write_note_update_uses_existing_path_from_plan_not_from_model`). |
| `existing_path` | `str` | Дефолт `""`. Обязателен при `action="update"` по идее схемы — на практике реальный путь всё равно берётся из `OutlineNote.existing_path`. |
| `title` | `str` | Обязательное — фактически тоже игнорируется в пользу `note.title` из плана. |
| `folder` | `str` | Дефолт `""`. |
| `frontmatter_extra` | `list[FrontmatterField]` | `default_factory=list`. Игнорируется кодом. |
| `body_md` | `str` | Дефолт `""`. Тело заметки для `action="create"`. |
| `tags` | `list[str]` | `default_factory=list`. |
| `links_out` | `list[str]` | `default_factory=list`. Заголовки/пути других заметок. |
| `append_section` | `str` | Дефолт `""`. Если непусто и `action="update"` — добавляем блок, не переписываем всё. |

**Кто использует:** `roles/synthesizer_writer.py::write_note`
(`role="synthesizer_write"`) — конвертируется в `storage/models.py::DraftNote`
через `_to_draft_note`.

### `class SynthesisOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `notes` | `list[DraftNoteOutput]` | `default_factory=list`. |

**Примечание:** задел на гипотетический ПАКЕТНЫЙ ответ Synthesizer-а на
несколько заметок сразу — в АКТИВНОМ пайплайне
`roles/synthesizer_writer.py::write_note` делает ОДИН вызов на ОДНУ заметку
и получает `DraftNoteOutput` напрямую, не через `SynthesisOutput`.

## 5. Critic — ревью уже написанной заметки

### `class CriticVerdictOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `verdict` | `Literal["ok", "rewrite"]` | Итоговый вердикт. |
| `feedback` | `str` | Дефолт `""`. Заполняется ТОЛЬКО при `verdict="rewrite"` — конкретные, адресуемые замечания (см. `prompts.md §2`). |

**Кто использует:** `roles/critic.py::review_draft` (`role="critic"`,
`../roles/critic.md`).

## 6. Папки для заметок

### `class FolderAssignmentItem(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `index` | `int` | Индекс заметки в батче (ЛОКАЛЬНЫЙ). |
| `folder` | `str` | Либо точное имя существующей папки, либо папка по умолчанию — модель НИКОГДА не должна придумывать новые папки (см. `prompts.md §4`). |

### `class FolderAssignmentOutput(BaseModel)`

| Поле | Тип | Назначение |
|---|---|---|
| `items` | `list[FolderAssignmentItem]` | `default_factory=list`. |

**Кто использует:** `roles/vault_analyst.py::_assign_folders_batch`
(`role="folder_assignment"`). Код применяет "safety net" ПОВЕРХ этого
ответа: если модель пропустила заметку в ответе ИЛИ вернула папку не из
допустимого множества — такой заметке принудительно проставляется
`default_folder` (см. `../roles/vault_analyst.md`).

Документация по `llm/schemas.py` завершена.
