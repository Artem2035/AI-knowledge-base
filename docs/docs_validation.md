# Документация: папка `validation/`

> Финальный детерминированный (БЕЗ LLM) слой проверки ПЕРЕД staging. Ключевой
> архитектурный принцип проекта (см. `docs/architecture.md §2.2`, роль
> Validator): LLM не должен проверять то, что можно надёжно проверить
> программно. Всё, что здесь есть, — чистые функции над уже готовыми
> `DraftNote`/`StagingChangeset`, без единого сетевого или LLM-вызова.

---

## 1. `validation/__init__.py` — точка входа и сборка отчёта

### 1.1. `validate_no_unauthorized_deletes(changeset: StagingChangeset, allow_delete: bool) -> list[ValidationIssue]`

**Описание.** Единственная проверка на уровне ВСЕГО changeset-а (не по
отдельным драфтам). Если `changeset.deletes` непуст, а `allow_delete=False` —
возвращает ОДНУ `ValidationIssue` уровня `"error"` с кодом
`"delete_not_allowed"` (без привязки к конкретному `draft_id` — `None`,
т.к. это не про конкретную заметку, а про весь список удалений).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Проверяемый changeset. |
| `allow_delete` | `bool` | Из `settings.allow_delete` (дефолт `False`). |

**Возвращаемое значение:** `list[ValidationIssue]` — либо пустой список (если
`deletes` пуст ИЛИ `allow_delete=True`), либо список из ОДНОГО элемента.

**Исключения:** не поднимает.

### 1.2. `run_validation(changeset: StagingChangeset, db: VaultDB, allow_delete: bool, plan: Plan | None = None) -> ValidationReport`

**Описание.** Главная точка входа — единственная функция, вызываемая
`Orchestrator.run()` (на этапе `"validating"`) и `cli/main.py` косвенно (через
уже сохранённый в changeset-е `ValidationReport`). Порядок проверок:
1. `validate_no_unauthorized_deletes(changeset, allow_delete)`.
2. `validate_no_path_collisions(all_drafts, db)` (`link_validator.py`) — где
   `all_drafts = changeset.creates + changeset.updates`.
3. `validate_links(all_drafts, db)` (`link_validator.py`).
4. Для КАЖДОГО черновика в `all_drafts` (цикл `for draft in all_drafts`):
   - `validate_yaml_frontmatter(draft)` (`yaml_validator.py`);
   - `validate_markdown_body(draft)` (`markdown_validator.py`);
   - `validate_headings_coverage(draft, notes_by_id.get(draft.note_id))`
     (`markdown_validator.py`), где `notes_by_id` строится ИЗ `plan.notes`
     ПО `note_id` (`{n.note_id: n for n in plan.notes}` — пусто, если `plan`
     не передан).
5. `ok = not any(i.level == "error" for i in issues)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Проверяемый набор изменений. |
| `db` | `VaultDB` | Для сверки путей/заголовков против реального индекса Vault. |
| `allow_delete` | `bool` | Из `settings.allow_delete`. |
| `plan` | `Plan \| None` (дефолт `None`) | Опционален — БЕЗ него проверка покрытия заголовков (`validate_headings_coverage`) просто ничего не добавляет (не падает, `notes_by_id` пуст), см. регрессионный тест `test_run_validation_without_plan_skips_headings_coverage_safely`. |

**Возвращаемое значение:** `ValidationReport` — `ok` (bool) + полный список `issues`.

**Исключения:** не поднимает намеренно — сама функция не выполняет I/O,
кроме чтения через `db` (методы `VaultDB` теоретически могут поднять
`sqlite3.Error`, не перехвачено явно ни в одной из вызываемых здесь функций).

---

## 2. `validation/yaml_validator.py` — YAML-валидация

### 2.1. `_ALLOWED_SCALAR_TYPES: tuple`

**Описание.** `(str, int, float, bool, type(None))` — типы, допустимые как
значение или элемент списка в frontmatter. Всё, что не входит в этот кортеж
(например, вложенный `dict`, произвольный объект) — считается неподдерживаемым
типом.

### 2.2. `validate_yaml_frontmatter(draft: DraftNote) -> list[ValidationIssue]`

**Описание.** Две независимые проверки:

**(а) Типы значений frontmatter** — локальная вложенная функция
`_check_value(key, value)`: если `value` — список, каждый элемент списка
должен быть одним из `_ALLOWED_SCALAR_TYPES` (иначе — `error`,
`yaml_unsupported_type`, с указанием `type(v)`); если `value` — не список,
проверяется, что сам он один из `_ALLOWED_SCALAR_TYPES` (иначе аналогичная
ошибка, но без "в списке"). Проверяются ВСЕ ключи `draft.frontmatter`.

**(б) Реальный YAML round-trip** — `dumped = yaml.safe_dump(draft.frontmatter,
allow_unicode=True)`, затем `yaml.safe_load(dumped)` — гарантия, что
frontmatter ДЕЙСТВИТЕЛЬНО сериализуется и парсится обратно (страховка сверх
проверки типов — например, от объектов, которые технически являются
допустимым типом Python, но дают невалидный YAML). Любое исключение здесь —
`error`, `yaml_roundtrip_failed`, с текстом исходного исключения.

**(в) Пустой заголовок** — если `draft.title.strip()` пуст — `error`,
`empty_title`.

**Параметры:** `draft: DraftNote`.

**Возвращаемое значение:** `list[ValidationIssue]` — все найденные проблемы
(может быть несколько за один вызов — например, и `yaml_unsupported_type`, и
`empty_title` одновременно).

**Исключения:** не поднимает — `yaml.safe_dump`/`yaml.safe_load` обёрнуты в
`try/except Exception`, ошибка становится `ValidationIssue`, а не пробрасывается.

---

## 3. `validation/markdown_validator.py` — Markdown-валидация

### 3.1. Модульные объекты

| Имя | Значение | Назначение |
|---|---|---|
| `_md` | `MarkdownIt("commonmark")` | Единый парсер CommonMark (переиспользуется между вызовами, не пересоздаётся). |
| `_CODE_FENCE_RE` | `` ```.*?``` `` (`re.DOTALL`) | Не используется активно в приведённом коде функций модуля (объявлен, но фактическая логика подсчёта code fence — через `text.count("```")`, см. ниже). |

### 3.2. `_count_substantial_paragraphs(text: str, min_chars: int = 40) -> int` (приватная)

**Описание.** Парсит текст через `_md.parse(text)` (токены CommonMark),
считает количество параграфов (`inline`-токенов, ИДУЩИХ СРАЗУ ПОСЛЕ
`paragraph_open`), чей `.content.strip()` длиннее `min_chars` символов —
т.е. "содержательных" абзацев, а не формальных пустых/односложных.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `text` | `str` | Тело заметки. |
| `min_chars` | `int` (дефолт `40`) | Минимальная длина абзаца, чтобы считаться "содержательным". |

**Возвращаемое значение:** `int` — количество содержательных абзацев.
**Исключения:** не поднимает (кроме теоретических сбоев `_md.parse`, которые в норме не должны происходить на произвольном тексте CommonMark).

### 3.3. `validate_markdown_body(draft: DraftNote) -> list[ValidationIssue]`

**Описание.** Последовательность проверок:
1. **Пустое тело** — если `draft.body_md.strip()` пуст И `draft.append_section`
   тоже пуст — `error`, `empty_body`, функция ВОЗВРАЩАЕТСЯ НЕМЕДЛЕННО
   (дальнейшие проверки бессмысленны без текста).
2. `text = draft.body_md or draft.append_section or ""` — источник текста для
   ВСЕХ дальнейших проверок (для `update`-черновиков это именно ДОБАВЛЯЕМЫЙ
   блок, не весь файл).
3. **Незакрытый code fence** — `text.count("```") % 2 != 0` → `error`,
   `unbalanced_code_fence` (нечётное число тройных бэктиков — где-то не
   закрыт блок кода).
4. **Markdown парсится** — `_md.parse(text)` в `try/except Exception`; при
   сбое — `error`, `markdown_parse_error`, с текстом исходного исключения.
5. **Длина текста:**
   - `len(text) < 40` → `warning`, `very_short_note` (возможно, стоит
     объединить с другой заметкой).
   - Иначе, ТОЛЬКО для `draft.action == NoteAction.CREATE`: если
     `_count_substantial_paragraphs(text) < 3` → `warning`,
     `note_too_short_structural` (рекомендуемый минимум 3 содержательных
     абзаца для НОВОЙ заметки; для `update` эта проверка НЕ выполняется —
     добавляемый блок может быть короче по своей природе).

**Параметры:** `draft: DraftNote`.

**Возвращаемое значение:** `list[ValidationIssue]`.

**Исключения:** не поднимает — ошибка парсинга Markdown перехватывается и
превращается в `ValidationIssue` уровня `error`.

### 3.4. `validate_headings_coverage(draft: DraftNote, note: OutlineNote | None) -> list[ValidationIssue]`

**Описание.** Сверяет, что ВСЕ заголовки `##`, заявленные планом
(`note.subpoints[*].heading`), реально присутствуют в тексте заметки. Если
`note is None` (план не передан в `run_validation`, ЛИБО `draft.note_id` не
найден в `notes_by_id` — например, объединённый черновик из
`staging/draft_merge.py` с `note_id=""`) — возвращает `[]` немедленно, БЕЗ
проверки (не ошибка, сознательный no-op).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `draft` | `DraftNote` | Проверяемый черновик. |
| `note` | `OutlineNote \| None` | Соответствующий узел плана (по `note_id`), либо `None`. |

**Возвращаемое значение:** `list[ValidationIssue]` — по одному `warning`
(`missing_outline_heading`) на КАЖДЫЙ заголовок из `note.subpoints`,
отсутствующий в тексте (проверка простым `f"## {sp.heading}" not in text`,
где `text = draft.body_md or draft.append_section or ""`). НИКОГДА не даёт
`error` — это ВСЕГДА `warning`, не блокирует `approve` (см. §4 ниже про
уровни серьёзности).

**Исключения:** не поднимает.

---

## 4. `validation/link_validator.py` — валидация связей и путей

### 4.1. `validate_links(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]`

**Описание.** Для КАЖДОЙ исходящей ссылки (`d.links_out`) КАЖДОГО черновика
проверяет, ссылается ли она на РЕАЛЬНО существующий заголовок — либо уже в
Vault (`db.get_all_notes()` → `{row["title"]}`), либо среди заголовков ЭТОГО
ЖЕ батча черновиков (`draft_titles = {d.title for d in drafts}`). Объединение
двух множеств — `known_titles`. Дополнительно строится
`known_normalized = {normalize_link_title(t): t for t in known_titles}`
(`tools/markdown_tools.py::normalize_link_title` — сравнение без учёта
вариантов дефиса).

**Логика для каждой ссылки `linked_title`:**
1. Если `linked_title in known_titles` (точное совпадение) — пропускается, всё в порядке.
2. Иначе — проверяется `normalize_link_title(linked_title)` в `known_normalized`:
   если найдено совпадение ПОСЛЕ нормализации — это, СКОРЕЕ ВСЕГО, та же самая
   ссылка, которую `roles/synthesizer_writer.py::_to_draft_note` УЖЕ должен
   был снаппить к точному имени. Если сюда всё же дошло — это БАГ снаппинга,
   а не намеренная "красная" ссылка → `warning`,
   `wikilink_dash_variant_mismatch` ("отличаются только Unicode-варианты
   дефиса. Проверьте вручную").
3. Иначе (совпадений нет вообще) — `warning`, `broken_wikilink` ("будет
   создана 'красная' ссылка в Obsidian" — НЕ ошибка, это штатное поведение
   Obsidian для ссылок на ещё не созданные заметки).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Обычно `changeset.creates + changeset.updates`. |
| `db` | `VaultDB` | Источник заголовков реально существующих заметок Vault. |

**Возвращаемое значение:** `list[ValidationIssue]` — ВСЕГДА уровня `warning`
(и `broken_wikilink`, и `wikilink_dash_variant_mismatch`) — НИКОГДА не
блокирует `approve`.

**Исключения:** не поднимает.

### 4.2. `validate_no_path_collisions(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]`

**Описание.** Три независимые проверки путей, накапливаемые в общий список:

1. **`create` на уже существующий путь** — если `d.action.value == "create"`
   и `d.path in existing_paths` (`db.get_all_paths()`) → `error`,
   `create_collides_with_existing` ("Должно было быть `action=update`").
2. **`update` на несуществующий путь** — если `d.action.value == "update"` и
   `d.path not in existing_paths` → `error`, `update_missing_target`.
3. **Дублирующийся путь ВНУТРИ ОДНОГО changeset-а** — `seen_in_batch: dict[str, str]`
   отслеживает пути, уже встреченные в ЭТОМ ЖЕ проходе; если `d.path` уже в
   `seen_in_batch` — `error`, `duplicate_path_in_changeset` ("Два черновика в
   одном changeset нацелены на один и тот же путь"). Эта проверка идёт
   ПОСЛЕДНЕЙ в цикле, но накопление `seen_in_batch[d.path] = d.draft_id`
   происходит БЕЗУСЛОВНО на каждой итерации (даже если для этого `path` уже
   были подняты другие issues выше).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Все черновики changeset-а. |
| `db` | `VaultDB` | Источник `existing_paths`. |

**Возвращаемое значение:** `list[ValidationIssue]` — ВСЕГДА уровня `error`
(все три проверки этой функции — блокирующие).

**Исключения:** не поднимает.

---

## Сводная таблица: все коды `ValidationIssue`, их уровень и источник

| `code` | `level` | Файл/функция | Блокирует `approve`? |
|---|---|---|---|
| `delete_not_allowed` | `error` | `validation/__init__.py::validate_no_unauthorized_deletes` | Да |
| `create_collides_with_existing` | `error` | `link_validator.py::validate_no_path_collisions` | Да |
| `update_missing_target` | `error` | `link_validator.py::validate_no_path_collisions` | Да |
| `duplicate_path_in_changeset` | `error` | `link_validator.py::validate_no_path_collisions` | Да |
| `broken_wikilink` | `warning` | `link_validator.py::validate_links` | Нет |
| `wikilink_dash_variant_mismatch` | `warning` | `link_validator.py::validate_links` | Нет |
| `yaml_unsupported_type` | `error` | `yaml_validator.py::validate_yaml_frontmatter` | Да |
| `yaml_roundtrip_failed` | `error` | `yaml_validator.py::validate_yaml_frontmatter` | Да |
| `empty_title` | `error` | `yaml_validator.py::validate_yaml_frontmatter` | Да |
| `empty_body` | `error` | `markdown_validator.py::validate_markdown_body` | Да |
| `unbalanced_code_fence` | `error` | `markdown_validator.py::validate_markdown_body` | Да |
| `markdown_parse_error` | `error` | `markdown_validator.py::validate_markdown_body` | Да |
| `very_short_note` | `warning` | `markdown_validator.py::validate_markdown_body` | Нет |
| `note_too_short_structural` | `warning` | `markdown_validator.py::validate_markdown_body` | Нет |
| `missing_outline_heading` | `warning` | `markdown_validator.py::validate_headings_coverage` | Нет |

**Принцип уровней (по всей папке `validation/`):** `error` — то, что физически
СЛОМАЕТ Vault/Obsidian или нарушит явный запрет пользователя (пустые файлы,
битый YAML, коллизии путей, несанкционированное удаление). `warning` — то,
что технически корректно, но заслуживает ВНИМАНИЯ пользователя ПЕРЕД
`approve` (короткие заметки, красные ссылки, неполное покрытие плана) — эти
issues видны в `staging/diff.py::render_diff_summary`, но НЕ блокируют запись.

---

## Сводная схема вызова `run_validation`

```
Orchestrator.run()  (этап "validating", после merge_confirm_cb)
        │
        ▼
validation/__init__.py::run_validation(changeset, db, allow_delete, plan)
        │
        ├─► validate_no_unauthorized_deletes(changeset, allow_delete)
        │
        ├─► validate_no_path_collisions(all_drafts, db)      ── link_validator.py
        ├─► validate_links(all_drafts, db)                    ── link_validator.py
        │
        └─► для каждого draft:
                ├─► validate_yaml_frontmatter(draft)           ── yaml_validator.py
                ├─► validate_markdown_body(draft)              ── markdown_validator.py
                └─► validate_headings_coverage(draft, note)     ── markdown_validator.py
        │
        ▼
ValidationReport(ok=not any(error), issues=[...])
        │
        ▼
changeset.validation = report  →  staging/changeset.py::save_changeset(...)
        │
        ▼
staging/diff.py::render_diff_summary(changeset)  — показывает issues пользователю
```

Документация по `validation/` завершена.

---

## Итог: все документированные папки проекта

1. `docs_llm_cycle_part1_orchestrator.md` — `cli/main.py` → `Orchestrator` → `LLMBudget` → `llm/factory.py` → `llm/base.py`.
2. `docs_llm_cycle_part2_common.md` — `llm/common.py`.
3. `docs_llm_cycle_part3_groq_client.md` — `llm/groq_client.py` целиком.
4. `docs_roles_part1.md` — `roles/outline_planner.py`, `roles/researcher.py`, `roles/extractor_critic.py`.
5. `docs_roles_part2.md` — `roles/elaborator.py`, `roles/vault_analyst.py`, `roles/synthesizer_writer.py`, `roles/critic.py`.
6. `docs_storage_models.md` — `storage/models.py`.
7. `docs_staging.md` — `staging/changeset.py`, `staging/checkpoint.py`, `staging/commit.py`, `staging/diff.py`, `staging/draft_merge.py`.
8. `docs_cli.md` — `cli/main.py` (полная версия команд), `cli/plan_editor.py`, `cli/draft_merge_editor.py`.
9. `docs_vault.md` — `vault/db.py`, `vault/reader.py`, `vault/index.py`, `vault/writer.py`.
10. `docs_tools.md` — `tools/web_search.py`, `tools/web_fetch.py`, `tools/markdown_tools.py`, `tools/dedup.py`.
11. `docs_validation.md` — `validation/__init__.py`, `validation/yaml_validator.py`, `validation/markdown_validator.py`, `validation/link_validator.py`.

Не документированы (по вашему изначальному запросу — вне текущего охвата):
`retrieval/search.py`, `config/settings.py` (упоминается контекстно в других
файлах, но не разобран целиком), `llm/schemas.py`, `llm/prompts/*`,
`llm/router.py`, `llm/openrouter_client.py`, `llm/chunking.py`. Если нужно —
могу продолжить в том же формате.
