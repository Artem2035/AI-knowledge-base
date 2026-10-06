# Документация: `validation/link_validator.py`

> Reference-док. Обзор пакета — `_index.md`.

## `validate_links(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]`

Для каждой ссылки из `d.links_out` проверяет, что имя известно. Известные имена:
- заголовки заметок Vault (`db.get_all_notes()`);
- заголовки предлагаемых черновиков (в том числе MOC);
- **stem путей** существующих (`db.get_all_paths()`) и предлагаемых файлов. Obsidian разрешает `[[ссылку]]` по имени файла, поэтому ссылка на update-заметку по stem (`prepare_linking_context`) или на файл с заголовком, отличным от имени, не считается битой.

Для ссылки `linked_title`:
1. точное совпадение → ок;
2. совпадение только после `normalize_link_title` → `warning` `wikilink_dash_variant_mismatch` (снаппинг в аннотаторе должен был это поймать, значит баг);
3. иначе → `warning` `broken_wikilink` («красная» ссылка Obsidian).

**Возвращает:** только `warning`, approve не блокируется. **Исключения:** `sqlite3.Error` из `db` не перехватывается.

## `validate_no_path_collisions(drafts, db) -> list[ValidationIssue]`

Три проверки (все `error`, блокируют approve):
1. `create` на существующий путь → `create_collides_with_existing`;
2. `update` на несуществующий путь → `update_missing_target`;
3. два черновика на один путь в одном changeset → `duplicate_path_in_changeset`.

Для MOC коллизию с Vault и другими черновиками предотвращает `build_moc` (суффикс « (2)»), эта проверка остаётся страховкой.

Документация по `validation/link_validator.py` завершена. Пакет закрыт, обзор — `_index.md`.