# `validation/` — обзор пакета

## Назначение пакета

Финальный детерминированный слой проверки ПЕРЕД staging, без LLM (принцип проекта: LLM не проверяет то, что можно надёжно проверить кодом). Чистые функции над `DraftNote`/`StagingChangeset` (`../storage/models.md`).

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `validation/__init__.py` | `_index.md` (этот файл — покрывает `run_validation`, точку сборки трёх валидаторов) |
| `validation/yaml_validator.py` | `yaml_validator.md` |
| `validation/markdown_validator.py` | `markdown_validator.md` |
| `validation/link_validator.py` | `link_validator.md` |

## Зависимости

```
Orchestrator.run() (этап "validating", после merge_confirm_cb)
   └─► run_validation(changeset, db, allow_delete, plan)
         ├─► validate_no_unauthorized_deletes
         ├─► link_validator.validate_no_path_collisions / validate_links
         └─► для каждого draft:
               yaml_validator.validate_yaml_frontmatter
               markdown_validator.validate_markdown_body
               markdown_validator.validate_headings_coverage
   ─► ValidationReport ─► changeset.validation ─► staging/diff.py
```

## 1. `validate_no_unauthorized_deletes(changeset, allow_delete) -> list[ValidationIssue]`

Проверка уровня всего changeset. Если `changeset.deletes` непуст при `allow_delete=False` — одна `error` с кодом `delete_not_allowed` (без `draft_id`). Иначе `[]`. Не поднимает.

## 2. `run_validation(changeset, db, allow_delete, plan=None) -> ValidationReport`

Единственная точка входа, вызывается `Orchestrator.run()`. Порядок:
1. `validate_no_unauthorized_deletes`;
2. `validate_no_path_collisions(all_drafts, db)`;
3. `validate_links(all_drafts, db)`;
4. для каждого черновика (`all_drafts = creates + updates`): YAML, тело Markdown, покрытие заголовков (`notes_by_id` строится из `plan.notes` по `note_id`);
5. `ok = not any(level == "error")`.

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Проверяемый набор изменений. |
| `db` | `VaultDB` | Сверка путей/заголовков (`../vault/db.md`). |
| `allow_delete` | `bool` | `settings.allow_delete`. |
| `plan` | `Plan \| None` | Без него проверка заголовков — no-op (не падает). |

**Возвращает:** `ValidationReport(ok, issues)`. Сама функция I/O не делает (кроме чтения через `db`; `sqlite3.Error` не перехватывается).

## Сводная таблица кодов

| `code` | `level` | Источник | Блокирует approve |
|---|---|---|---|
| `delete_not_allowed` | error | `__init__` | Да |
| `create_collides_with_existing` | error | link_validator | Да |
| `update_missing_target` | error | link_validator | Да |
| `duplicate_path_in_changeset` | error | link_validator | Да |
| `broken_wikilink` | warning | link_validator | Нет |
| `wikilink_dash_variant_mismatch` | warning | link_validator | Нет |
| `yaml_unsupported_type` | error | yaml_validator | Да |
| `yaml_roundtrip_failed` | error | yaml_validator | Да |
| `empty_title` | error | yaml_validator | Да |
| `empty_body` | error | markdown_validator | Да |
| `unbalanced_code_fence` | error | markdown_validator | Да |
| `markdown_parse_error` | error | markdown_validator | Да |
| `very_short_note` | warning | markdown_validator | Нет |
| `note_too_short_structural` | warning | markdown_validator | Нет |
| `missing_outline_heading` | warning | markdown_validator | Нет |

**Принцип уровней:** `error` — то, что сломает Vault/Obsidian или нарушит запрет пользователя; `warning` — корректно технически, но требует внимания перед approve (виден в `../staging/diff.md`).

## Порядок чтения

`link_validator.md` → `yaml_validator.md` → `markdown_validator.md`.

Документация по `validation/__init__.py` завершена.
