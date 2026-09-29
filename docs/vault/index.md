# Документация: `vault/index.py` — инкрементальная индексация

> Reference-док. ⚠ Это документ модуля `vault/index.py`, не путать с `_index.md` (обзор пакета).

**Назначение.** Если для файла в `notes` уже есть запись с тем же `content_hash` — файл не перепарсивается и эмбеддинг не пересчитывается. Это позволяет не пересканировать Vault целиком.

## `_now() -> str` (приватная)
UTC ISO-время (локальная копия `storage/models.py::_now`).

## `class VaultIndexer`

### `__init__(self, db: VaultDB, vault_path: Path, embedder=None)`
`embedder` — `tools.dedup.LocalEmbedder | None` (`../tools/dedup.md`); если задан, эмбеддинги считаются при переиндексации.

### `sync(self) -> dict`
1. `read_vault` (`reader.md`) → все заметки с диска.
2. Для каждой: если `db.note_exists_with_hash` — `unchanged`, иначе `_upsert` — `updated`.
3. Пути из индекса, которых нет на диске, удаляются через `db.delete_note` (`db.md`) — заметка убрана пользователем вручную; `ALLOW_DELETE` тут не применим.

**Возвращает:** `{"scanned", "updated", "unchanged", "removed"}` (`scanned` — сколько файлов реально прочитано). **Исключения:** `FileNotFoundError`, `sqlite3.Error`.

### `_upsert(self, note: ParsedNote) -> None` (приватный)
Пересчитывает `summary`, вызывает `db.upsert_note`, сохраняет исходящие ссылки с `target_path=None` (первый проход). Эмбеддинг (`title\nsummary`) — best-effort: любое исключение логируется как warning, индексацию не ломает.

### `resolve_wikilink_targets(self) -> None`
Второй проход: строит `title → path` по всему индексу и пересобирает ссылки каждой заметки с резолвленными `target_path` (`None` = красная ссылка). Нужен для `db.get_backlinks`.

**Важно:** вызывается ОТДЕЛЬНО от `sync()`, всегда следом (`Orchestrator.sync_vault_index`, `staging/commit.py`, `cli/main.py::index`), т.к. зависит от того, что все заметки уже в индексе.

Документация по `vault/index.py` завершена. Далее — `writer.md`.
