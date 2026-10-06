# Документация: `storage/models.py`

> Reference-док. Обзор пакета — `_index.md`. Единственный модуль со структурированными объектами, которыми обмениваются этапы workflow.

**Назначение.** Между ролями не передаётся длинный «сырой» текст, только эти Pydantic-модели. Это даёт: (1) валидацию на границах, (2) JSON-сериализацию для персистентного прогресса (`../staging/checkpoint.md`), (3) предсказуемый контракт для structured-output (`../llm/schemas.md` — отдельные «выходные» модели, LLM эти не заполняет).

## 0. Служебные функции

### `_now() -> str`
UTC, ISO 8601. `default_factory` для `created_at`/`timestamp`.

### `_new_id() -> str`
Первые 12 hex-символов UUID4. `default_factory` всех `*_id`. Принцип проекта: id **всегда** генерирует код, модель ссылается на элементы по локальному индексу в промпте.

---

## 1. Task / Plan

### 1.0. Домен и тип раздела

| Имя | Что это |
|---|---|
| `Domain` | `Literal["technical", "humanities", "life_management"]`. |
| `Kind` | `Literal[...]` из 18 значений: technical — `definition`, `mechanism`, `parameters`, `example`, `comparison`, `pitfalls`; humanities — `context`, `key_idea`, `interpretations`, `terms_persons`, `critique`, `connections`; life_management — `principle`, `when_to_apply`, `steps`, `scenario`, `mistakes`, `checklist`; универсальный `other`. |
| `DEFAULT_DOMAIN` | `"technical"`. |
| `DOMAIN_KINDS` | `dict[str, tuple[str, ...]]` — **единственный источник истины**, какие `kind` допустимы в каком домене (без `other`, он допустим всегда). Используется `roles/outline_planner.py`, `cli/plan_editor.py`. |

#### `normalize_kind(kind: str, domain: str) -> str`
`kind`, не входящий в набор домена (или неизвестный домен) → `"other"`. Не поднимает.

### 1.1. `class Task(BaseModel)`
`task_id` (`_new_id`; при resume передаётся явно), `raw_query: str`, `language = "ru"`, `created_at`.

### 1.2. `class OutlineSubpoint(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `subpoint_id` | `str` | `_new_id`; стабильный id для resume. |
| `heading` | `str` | Текст заголовка `##` в заметке. |
| `covers` | `str` | Техзадание для Elaborator (что раскрыть, не текст). |
| `kind` | `Kind` | Дефолт `"other"` (старые планы без поля загружаются). Допустимые значения зависят от `Plan.domain`. |

### 1.3. `class OutlineNote(BaseModel)`
`note_id`, `title` (зафиксирован планом), `subpoints`, `rationale = ""`. Поля `action: Literal["create","update"] = "create"`, `existing_path = ""`, `folder = ""` заполняет не Planner, а `roles/vault_analyst.py::resolve_notes_against_vault` (мутация на месте).

### 1.4. `class Plan(BaseModel)`
| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Связь с `Task`. |
| `topic_title` | `str` | Название темы; идёт в заголовок MOC и в папку темы. |
| `summary` | `str` | Абзац-резюме (правило 7 планнера); в MOC выводится как `[!abstract] Кратко`. Дефолт `""`. |
| `domain` | `Domain` | Определяет планнер, правит пользователь в `cli/plan_editor.py`. Влияет на инструкцию Elaborator, humanities-предупреждение и тег `domain/<домен>`. |
| `notes` | `list[OutlineNote]` | Дерево плана. |

---

## 2. Секции, аннотации и Evidence

### 2.1. `class Evidence(BaseModel)` — **legacy**
Атомарное утверждение (`note_id`, `subpoint_id`, `statement`, `source_id`, `confidence`, `is_definition`, `critic_note`, `verified`). В активном пути **не используется** (цепочка Evidence → Writer → Critic заменена на `SectionDraft`); оставлена для сломанных web-ролей и старых данных. Также остаётся `SourceCandidate` (результат веб-поиска, только web-режим).

### 2.2. `class SectionDraft(BaseModel)`
Готовый markdown одного подпункта (результат Elaborator v2).

| Поле | Тип | Назначение |
|---|---|---|
| `note_id` | `str` | Заметка плана. |
| `subpoint_id` | `str` | Подпункт. |
| `markdown` | `str` | Тело раздела без заголовка `##`. |
| `needs_check` | `bool` | Модель не уверена в деталях (или раздел — placeholder); попадает в `DraftNote.unverified_sections`, в callout заметки и в diff. |

Хранится в `TaskCheckpoint.sections`.

### 2.3. `class NoteAnnotation(BaseModel)`
Результат Annotator для одной заметки: `note_id`, `tags`, `links_out`, `abstract = ""` (пусто, если резюме не запрашивалось или модель его не вернула). Хранится в `TaskCheckpoint.annotations`.

---

## 3. Vault Analyst

### 3.1. `class ExistingNote(BaseModel)`
Полный/архивный формат существующей заметки (`path`, `title`, `frontmatter`, `tags`, `summary`, `content_hash`, `similarity_score`, `matched_concept`, `decision`). В активном пути вместо него используется лёгкий `RetrievalHit` (`retrieval/search.py`).

---

## 4. Заметки

### 4.1. `class NoteAction(str, Enum)`
`CREATE = "create"`, `UPDATE = "update"`.

### 4.2. `class DraftNote(BaseModel)`
Черновик одной заметки: результат `tools/note_assembly.py::build_draft_note`, единица staging и diff.

| Поле | Тип | Назначение |
|---|---|---|
| `draft_id` | `str` | `_new_id`. |
| `note_id` | `str` | Дефолт `""`. Пусто у объединённых черновиков и MOC (для них `validate_headings_coverage` — no-op). |
| `action` | `NoteAction` | Обязательное. |
| `path` | `str` | CREATE — новый путь; UPDATE — путь существующей заметки. |
| `title`, `folder` | `str` | Заголовок и папка. |
| `frontmatter` | `dict` | При рендере ограничен ключами `title`/`tags`/`created`/`source` (`../tools/markdown_tools.md`). |
| `body_md` | `str` | Тело для CREATE. |
| `tags` | `list[str]` | Включает `domain/<домен>`; diff этот тег не показывает в списке тегов, а выводит отдельной строкой «домен». |
| `links_out` | `list[str]` | Заголовки/stem других заметок (не URL). |
| `source_refs` | `list[str]` | URL источников, только web-режим. |
| `append_section` | `str \| None` | Для UPDATE: что дописывается. |
| `depth_hint` | `str` | Legacy, для трассируемости. |
| `critic_rounds` | `int` | **Legacy** (Critic удалён); для старых `changeset.json`. |
| `needs_review` | `bool` | **Legacy**; diff по-прежнему показывает пометку для старых данных. |
| `unverified_sections` | `list[str]` | Заголовки разделов с `needs_check`. |
| `merged_from` | `list[str]` | Заголовки исходных заметок при слиянии (`../staging/draft_merge.md`): по ним `fix_links_after_merge` перенаправляет чужие ссылки. |
| `is_moc` | `bool` | Заметка-оглавление, собранная `build_moc`: без секции «Связанные заметки», без проверки «слишком короткая», без inline-ссылок. |

### 4.3. `class Relationship(BaseModel)`
`from_note`, `to_note`, `link_type: Literal["wikilink","tag","backlink"] = "wikilink"`. Строит `roles/synthesizer_writer.py::build_relationships`.

---

## 5. Validation / Staging

### 5.1. `class ValidationIssue(BaseModel)`
`level: Literal["error","warning"]`, `code`, `message`, `draft_id: str | None`. `error` блокирует approve.

### 5.2. `class ValidationReport(BaseModel)`
`ok` (нет ни одной `error`), `issues`; свойства `errors`, `warnings`.

### 5.3. `class StagingChangeset(BaseModel)`
`task_id`, `created_at`, `creates`, `updates`, `deletes` (в MVP всегда пуст), `relationships`, `validation: ValidationReport | None`. `validation is None` значит «не проходил `run_validation`», `commit_changeset` откажет (`../staging/commit.md`).

---

## 6. Статус и бюджет

### 6.1. `class LLMCallLog(BaseModel)`
`role`, `timestamp`, `prompt_tokens_est = 0` (зарезервировано), `ok`, `error`. Создаётся `LLMBudget.register_call`.

### 6.2. `class TaskStatus(BaseModel)`
`task_id`, `stage` (`planning`, `elaborating`, `vault_analysis`, `annotating`, `assembling`, `validating`, `staged`, `stopped`), `llm_calls_used` (только текущая сессия, обнуляется даже при resume), `llm_calls_log`, `stopped_reason`, `finished`. Передаётся по ссылке через весь цикл вызова (`../flows/llm_cycle.md`).

---

## Сводная схема связей

```
Task ─► Plan(domain, summary) ─► OutlineNote ─► OutlineSubpoint(kind)
                                      │ note_id/subpoint_id
              (Vault Analyst мутирует action/existing_path/folder)
                                      ▼
   Elaborator ─► SectionDraft ─┐          Annotator ─► NoteAnnotation
                               ▼                          ▼
              tools/note_assembly.build_draft_note ─► DraftNote
                    │ (merge: merged_from)  │ (apply_inline_links)
                    ▼                       ▼
                 build_moc ─► DraftNote(is_moc) ─► Relationship
                                      ▼
        StagingChangeset ─► run_validation ─► ValidationReport ─► save_changeset ─► approve ─► commit
```

Документация по `storage/models.py` завершена. Обзор пакета — `_index.md`.