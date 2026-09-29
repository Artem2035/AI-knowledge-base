# Документация: `vault/db.py` — SQLite-индекс Vault

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** НЕ дублирование Vault, а лёгкий локальный кэш метаданных для быстрого retrieval без парсинга всех файлов на каждый запрос и без отправки Vault в LLM.

**Схема:**
```sql
notes(path PK, title, content_hash, raw_content, summary, created_at, updated_at)
frontmatter(note_path, key, value_json)   -- FK → notes ON DELETE CASCADE
tags(note_path, tag)                      -- FK → notes ON DELETE CASCADE
links(source_path, target_path, target_title, link_type)
embeddings(note_path, model, vector_json) -- PK (note_path, model)
```

## `class VaultDB`

Обёртка над `sqlite3.Connection`; поддерживает контекстный менеджер (`__exit__` → `close()`).

### `__init__(self, db_path: Path)`
Создаёт родительскую папку, открывает соединение (`row_factory=sqlite3.Row`), включает `PRAGMA foreign_keys=ON`, выполняет `SCHEMA` (идемпотентно). `db_path` — из `settings.db_path` (`../config/settings.md §7`). **Исключения:** `sqlite3.Error`.

### `close(self) -> None`
Закрывает соединение.

### `upsert_note(self, path, title, content_hash, raw_content, summary, frontmatter, tags, created_at, updated_at) -> None`
`INSERT ... ON CONFLICT(path) DO UPDATE` — обновляются все поля, КРОМЕ `created_at`. Затем `frontmatter` и `tags` пересоздаются полностью (DELETE+INSERT). Коммитит.

### `note_exists_with_hash(self, path: str, content_hash: str) -> bool`
`True`, если есть запись с ЭТИМ путём и ЭТИМ хэшем — ключ инкрементальности (`index.md`).

### `get_all_notes(self) -> list[sqlite3.Row]` / `get_note(self, path) -> Row | None`
Все строки `notes` / одна по пути. Используются `retrieval/search.py::VaultSearcher` и валидацией.

### `delete_note(self, path: str) -> None`
Удаляет запись из ВСЕХ таблиц (`notes`, `frontmatter`, `tags`, `links` по `source_path`, `embeddings`). Это синхронизация индекса с уже случившимся удалением на диске, а не удаление файла — `ALLOW_DELETE` не применим.

### `get_all_paths(self) -> set[str]`
Все пути индекса. Используется `index.sync` и `validation/link_validator.py`.

### `get_distinct_folders(self) -> list[str]`
Уникальные папки (`path.rsplit("/", 1)[0]`) из индекса, отсортированы; заметки в корне не дают записи. Нужна `roles/vault_analyst.py::_assign_folders_batch` (`../roles/vault_analyst.md`), чтобы переиспользовать существующую структуру папок.

### `set_links(self, source_path, links: list[tuple[str | None, str, str]]) -> None`
Полностью пересоздаёт исходящие ссылки: `(target_path_or_None, target_title, link_type)`. `target_path=None` — нерезолвленная («красная») ссылка.

### `get_backlinks(self, target_path) -> list[str]`
`source_path` заметок, ссылающихся на `target_path` (только уже резолвленные ссылки).

### `get_outlinks(self, source_path) -> list[str]`
`target_title` всех исходящих ссылок.

### `get_all_tags(self) -> list[str]`
Уникальные теги, отсортированы.

### `set_embedding(self, note_path, model, vector) -> None` / `get_embedding(self, note_path, model) -> list[float] | None` / `get_all_embeddings(self, model) -> dict[str, list[float]]`
Вектор хранится как JSON (`json.dumps`); upsert по `(note_path, model)`.

**Исключения всех методов:** `sqlite3.Error` — намеренно не перехватывается.

Документация по `vault/db.py` завершена. Далее — `index.md` (кто пишет в эту БД) или обзор пакета `_index.md`.
