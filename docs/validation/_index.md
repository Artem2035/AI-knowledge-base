# `validation/` — обзор пакета

## Назначение пакета

Финальный детерминированный слой проверки **перед staging**, без LLM (принцип проекта: то, что надёжно проверяет код, LLM не проверяет). Чистые функции над `DraftNote` / `StagingChangeset` (`../storage/models.md`). Перед проверкой простые конфликты путей исправляет `autofix.py`.

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `validation/__init__.py` | `_index.md` (этот файл: `run_validation`, точка сборки) |
| `validation/autofix.py` | `autofix.md` |
| `validation/yaml_validator.py` | `yaml_validator.md` |
| `validation/markdown_validator.py` | `markdown_validator.md` |
| `validation/link_validator.py` | `link_validator.md` |

## Зависимости

```
Orchestrator.run() (после сборки, слияния)
   ├─► autofix.autofix_drafts(drafts, existing_paths) ─► (drafts, path_autofixed warnings)
   │        ... inline-ссылки, MOC, relationships ...
   └─► run_validation(changeset, db, allow_delete, plan, extra_issues=autofix warnings)
         ├─► validate_no_unauthorized_deletes
         ├─► link_validator: validate_no_path_collisions, validate_links
         └─► для каждого draft (creates + updates):
               yaml_validator.validate_yaml_frontmatter
               markdown_validator.validate_markdown_body
               markdown_validator.validate_headings_coverage
   ─► ValidationReport ─► changeset.validation ─► staging/diff.py
```

## 1. `validate_no_unauthorized_deletes(changeset, allow_delete) -> list[ValidationIssue]`
`changeset.deletes` непуст при `allow_delete=False` → одна `error` `delete_not_allowed`. Иначе `[]`.

## 2. `run_validation(changeset, db, allow_delete, plan=None, extra_issues=None) -> ValidationReport`
Единственная точка входа. Порядок: предупреждения предыдущих шагов (`extra_issues`) → удаления → коллизии путей → ссылки → для каждого черновика YAML, тело Markdown, покрытие заголовков (`notes_by_id` строится из `plan.notes` по `note_id`). `ok = not any(level == "error")`.

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Проверяемый набор. |
| `db` | `VaultDB` | Пути и заголовки. |
| `allow_delete` | `bool` | `settings.allow_delete`. |
| `plan` | `Plan \| None` | Без него проверка заголовков — no-op. |
| `extra_issues` | `list[ValidationIssue] \| None` | Предупреждения предыдущих детерминированных шагов (`autofix`); добавляются в начало отчёта, чтобы попасть в diff. |

Для MOC и объединённых черновиков `note_id=""`, поэтому проверка покрытия заголовков для них пропускается; проверка структуры для MOC отключена (`is_moc`).

## Сводная таблица кодов

| `code` | `level` | Источник | Блокирует approve |
|---|---|---|---|
| `delete_not_allowed` | error | `__init__` | да |
| `create_collides_with_existing` | error | link_validator | да |
| `update_missing_target` | error | link_validator | да |
| `duplicate_path_in_changeset` | error | link_validator | да |
| `yaml_unsupported_type` | error | yaml_validator | да |
| `yaml_roundtrip_failed` | error | yaml_validator | да |
| `empty_title` | error | yaml_validator | да |
| `empty_body` | error | markdown_validator | да |
| `unbalanced_code_fence` | error | markdown_validator | да |
| `markdown_parse_error` | error | markdown_validator | да |
| `path_autofixed` | warning | autofix | нет |
| `broken_wikilink` | warning | link_validator | нет |
| `wikilink_dash_variant_mismatch` | warning | link_validator | нет |
| `very_short_note` | warning | markdown_validator | нет |
| `note_too_short_structural` | warning | markdown_validator | нет |
| `note_language_mismatch` | warning | markdown_validator | нет |
| `missing_outline_heading` | warning | markdown_validator | нет |

**Принцип уровней:** `error` — то, что сломает Vault/Obsidian или нарушит запрет пользователя; `warning` — технически корректно, но требует внимания перед approve (виден в `../staging/diff.md`).

**При ошибках** `Orchestrator` всё равно сохраняет changeset в staging (его видно командой `pending`), но оставляет чекпоинт: после устранения причины `resume <task_id>` пересобирает заметки и повторяет проверку без LLM-вызовов (`../orchestrator/state_machine.md`).

## Порядок чтения

`autofix.md` → `link_validator.md` → `yaml_validator.md` → `markdown_validator.md`.

Документация по `validation/__init__.py` завершена.