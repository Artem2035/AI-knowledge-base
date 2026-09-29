# Документация: `validation/yaml_validator.py`

> Reference-док. Обзор пакета — `_index.md`.

## `_ALLOWED_SCALAR_TYPES`
`(str, int, float, bool, type(None))` — допустимые типы значения (или элемента списка) во frontmatter. Всё остальное (вложенный `dict`, объекты) — неподдерживаемо.

## `validate_yaml_frontmatter(draft: DraftNote) -> list[ValidationIssue]`

Три независимые проверки:
1. **Типы.** Локальная `_check_value(key, value)`: для списка проверяется каждый элемент, иначе само значение; нарушение → `error`, `yaml_unsupported_type`. Проверяются все ключи `draft.frontmatter`.
2. **YAML round-trip.** `yaml.safe_dump(frontmatter, allow_unicode=True)` → `yaml.safe_load(...)`; любое исключение → `error`, `yaml_roundtrip_failed` (с текстом исключения).
3. **Заголовок.** Пустой `draft.title.strip()` → `error`, `empty_title`.

**Возвращает:** все найденные проблемы (может быть несколько). **Исключения:** не поднимает — сбои `safe_dump/safe_load` превращаются в `ValidationIssue`.

Примечание: реальный YAML при записи строит `../tools/markdown_tools.md::render_frontmatter` (только 4 разрешённых ключа); этот валидатор проверяет сырой `draft.frontmatter` до рендера.

Документация по `validation/yaml_validator.py` завершена. Далее — `markdown_validator.md`.
