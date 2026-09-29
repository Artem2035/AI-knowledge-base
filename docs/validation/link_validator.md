# Документация: `validation/link_validator.py`

> Reference-док. Обзор пакета — `_index.md`.

## `validate_links(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]`

Для каждой ссылки из `d.links_out` проверяет, существует ли заголовок: в Vault (`db.get_all_notes()`) или среди заголовков этого же батча (`known_titles`). Дополнительно строится `known_normalized` через `normalize_link_title` (`../tools/markdown_tools.md`).

Для ссылки `linked_title`:
1. точное совпадение с `known_titles` → ок;
2. совпадение только после нормализации → `warning`, `wikilink_dash_variant_mismatch` (должно было быть снаплено в `roles/synthesizer_writer.py::_to_draft_note`, значит баг снаппинга);
3. иначе → `warning`, `broken_wikilink` («красная» ссылка Obsidian — штатное поведение).

**Возвращает:** только `warning` — никогда не блокирует approve. **Исключения:** не поднимает.

## `validate_no_path_collisions(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]`

Три проверки (все `error`, все блокируют):
1. `create` на существующий путь (`db.get_all_paths()`) → `create_collides_with_existing`;
2. `update` на несуществующий путь → `update_missing_target`;
3. два черновика на один путь внутри changeset → `duplicate_path_in_changeset` (`seen_in_batch` пополняется безусловно на каждой итерации).

**Исключения:** не поднимает.

Документация по `validation/link_validator.py` завершена. Пакет закрыт — обзор `_index.md`.
