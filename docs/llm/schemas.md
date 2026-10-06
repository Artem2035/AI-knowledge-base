# Документация: `llm/schemas.py` — контракты structured-output

> Reference-док. Обзор пакета — `_index.md`. Промпты, использующие эти схемы — `prompts.md`. Батчинг списков под эти схемы — `chunking.md`.

**Назначение.** «Выходные» Pydantic-модели ответов LLM. Намеренно отделены от `storage/models.py` (`../storage/models.md`): модель не придумывает `task_id`/`path`/`note_id`/`subpoint_id` (это внешние ключи, ими управляет код). Вместо них она ссылается на **индексы** элементов, переданных в промпте (`unit_index`, `index`), а роль сама подставляет реальные id. Из `storage/models.py` импортируются только типы-литералы `Kind` и `Domain`, чтобы набор допустимых значений был в одном месте.

Простые типы (`str`/`float`/`bool`/`list`) вместо произвольных `dict`: JSON Schema с фиксированной формой полей надёжнее для structured output.

**Strict-режим Groq** (`openai/gpt-oss-*`, `../llm/groq_client.md §2.2`): `_to_strict_json_schema` помечает **все** поля как обязательные. Поэтому поля-индексы **намеренно без default** (`unit_index`, `index`): пропущенный индекс не должен молча превращаться в `0` и приписывать текст чужому разделу. Поля с default модель всё равно вернёт явно.

## 1. Planner

### `class OutlineSubpointOutput(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `heading` | `str` | Заголовок подпункта. |
| `covers` | `str` | Техзадание: что раскрыть, не сам текст. |
| `kind` | `Kind` (дефолт `"other"`) | Тип раздела. Допустимость для домена проверяет код (`normalize_kind`), не схема. |

### `class OutlineNoteOutput(BaseModel)`
`title: str`, `subpoints: list[OutlineSubpointOutput]`, `rationale: str = ""`.

### `class OutlinePlanOutput(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `topic_title` | `str` | Название темы. |
| `domain` | `Domain` (дефолт `"technical"`) | Домен темы. |
| `summary` | `str` (дефолт `""`) | Абзац-резюме темы (правило 7 промпта); идёт в MOC как `[!abstract] Кратко`. |
| `notes` | `list[OutlineNoteOutput]` | Заметки плана. |

**Кто использует:** `roles/outline_planner.py::build_plan` (`../roles/outline_planner.md`) → `storage/models.py::Plan`.

## 2. Elaborator v2 (`RESEARCH_MODE=knowledge`)

### `class SectionItem(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `unit_index` | `int` (обязательное) | Номер раздела в квадратных скобках в промпте батча (0-based). |
| `markdown` | `str` | Тело раздела **без** собственного заголовка (`## …` добавляет код). |
| `needs_check` | `bool` (дефолт `False`) | Модель не уверена в деталях раздела. |

### `class SectionBatchOutput(BaseModel)`
`sections: list[SectionItem]`.

**Кто использует:** `roles/elaborator.py` (`role="elaborator"`, `../roles/elaborator.md`) → `SectionDraft`. Прежние `ElaborationItem`/`ElaborationOutput` (факты-`Evidence`) удалены.

## 3. Vault dedup (только «серая зона»)

### `class DedupDecisionOutput(BaseModel)`
`same_concept: bool`, `decision: Literal["reuse", "extend", "distinct"]`, `reasoning: str = ""`. Используется `roles/vault_analyst.py::_resolve_ambiguous` (`role="vault_dedup"`).

## 4. Annotator

### `class AnnotationItem(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `index` | `int` (обязательное) | Номер заметки в квадратных скобках в промпте. |
| `tags` | `list[str]` | 2–5 тегов (нормализуются кодом, `normalize_tags`). |
| `links_out` | `list[str]` | 0–4 заголовка других заметок (чистятся кодом: только известные, без своего). |
| `abstract` | `str` (дефолт `""`) | Резюме 2–4 предложения, только если в промпте `abstract: required`. |

### `class AnnotationBatchOutput(BaseModel)`
`items: list[AnnotationItem]`.

**Кто использует:** `roles/annotator.py` (`role="annotator"`, `../roles/annotator.md`) → `NoteAnnotation`.

## 5. Папки для заметок

### `class FolderAssignmentItem(BaseModel)`
`index: int` (локальный в батче), `folder: str` — точное имя существующей папки или папка по умолчанию.

### `class FolderAssignmentOutput(BaseModel)`
`items: list[FolderAssignmentItem]`.

Используется `roles/vault_analyst.py::_assign_folders_batch` (`role="folder_assignment"`). Код применяет safety net поверх ответа: пропущенная или недопустимая папка заменяется на `default_folder` (`../roles/vault_analyst.md`). Заметьте: `index` здесь без default, но модель тоже может его пропустить — тогда заметка получит папку по умолчанию.

## 6. Web-режим (legacy, не в активном пути)

`SourceSelectionItem`/`SourceSelectionOutput` (`roles/researcher.py`) и `EvidenceItem`/`EvidenceBatchOutput` (`roles/extractor_critic.py`) оставлены для `RESEARCH_MODE=web`, который заблокирован в `Orchestrator.run()`. Не документируются подробно (`../CONTRIBUTING.md`).

## 7. Удалено

`ElaborationItem`, `ElaborationOutput`, `FrontmatterField`, `DraftNoteOutput`, `SynthesisOutput`, `CriticVerdictOutput` — вместе с ролями Writer и Critic.

Документация по `llm/schemas.py` завершена. Далее — `prompts.md`.