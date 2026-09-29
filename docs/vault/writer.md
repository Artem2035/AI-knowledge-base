# Документация: `vault/writer.py` — единственная точка записи в реальный Vault

> Reference-док. Обзор пакета — `_index.md`.

**КРИТИЧЕСКОЕ ПРАВИЛО ПРОЕКТА.** Модуль вызывается ТОЛЬКО из `staging/commit.py` (`../staging/commit.md`) после явного `approve`. Ничего в `roles/*`, `llm/*`, `retrieval/*` не должно импортировать его напрямую.

## `class WriteResult` (dataclass)

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Путь записанного файла. |
| `action` | `str` | `"create"` / `"update"`. |
| `backup_of` | `str \| None` | Путь snapshot «before» для UPDATE. |

## `class VaultWriter`

### `__init__(self, vault_path: Path, allow_delete: bool = False)`

### `write_draft(self, draft: DraftNote, backup_dir: Path | None = None) -> WriteResult`
**CREATE:** если файл существует — `FileExistsError` (защитная мера, должно ловиться валидацией); создаёт папки; пишет `render_markdown(draft)` (`../tools/markdown_tools.md`).
**UPDATE:** если файла нет — `FileNotFoundError`; при `backup_dir` сохраняет текущее содержимое в `backup_dir/<path с "/"→"__">`; при непустом `append_section` дописывает `"\n\n" + sanitize_wikilinks(section) + "\n"` к существующему тексту (старое не теряется), иначе перезаписывает через `render_markdown`.
**Исключения:** `FileExistsError`, `FileNotFoundError`, `OSError`.

### `delete_note(self, rel_path: str) -> None`
При `allow_delete=False` — `PermissionError` (не обходится автоматически). Если файла нет — тихо ничего.

### `ensure_folder(self, rel_folder: str) -> None`
`mkdir(parents=True, exist_ok=True)`; идемпотентно, вызывается перед каждым create.

Документация по `vault/writer.py` завершена. Пакет закрыт — обзор `_index.md`.
