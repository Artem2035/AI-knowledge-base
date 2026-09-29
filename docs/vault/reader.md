# Документация: `vault/reader.py` — чтение Vault через файловую систему

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** «Вариант A» (прямой доступ к ФС, без Obsidian API). Никогда не пишет в Vault — только читает.

## Регулярные выражения

| Имя | Назначение |
|---|---|
| `WIKILINK_RE` | Извлекает цель из `[[Title]]`, `[[Title#Heading]]`, `[[Title\|Alias]]` — группа 1 всегда чистый `Title`. |
| `INLINE_TAG_RE` | Инлайн-теги `#tag` (`(?<!\S)` — не срабатывает внутри слов/URL). Поддерживает кириллицу и `/`. |

## `class ParsedNote` (dataclass)

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Относительный POSIX-путь внутри Vault. |
| `title` | `str` | `frontmatter.title` или имя файла без расширения. |
| `frontmatter` | `dict` | YAML as-is. |
| `tags` | `list[str]` | Frontmatter + инлайн-теги, уникальные, отсортированы. |
| `body` | `str` | Тело без frontmatter. |
| `raw_content` | `str` | Полный файл. |
| `outlinks` | `list[str]` | Цели `[[wikilink]]`, отсортированы, без дублей. |
| `content_hash` | `str` | См. `_compute_hash`. |

## Функции

### `_compute_hash(raw_content: str) -> str` (приватная)
SHA-256 от UTF-8, первые 16 hex-символов.

### `_extract_tags(frontmatter_data: dict, body: str) -> list[str]` (приватная)
Теги из `frontmatter.tags` (строка или список, `lstrip("#")`) + инлайн-теги; объединение через `set`, сортировка.

### `_extract_wikilinks(body: str) -> list[str]` (приватная)
Уникальные цели `[[...]]`, отсортированы.

### `_make_summary(body: str, max_chars: int = 400) -> str`
Детерминированная summary без LLM: непустые строки без `#`-заголовков через пробел, обрезка по границе слова с `…`. Экспортируется в `__all__` и переиспользуется в `index.md`.

### `parse_note_file(vault_path: Path, file_path: Path) -> ParsedNote`
Читает файл, парсит `python-frontmatter`, строит относительный путь, извлекает теги/ссылки, считает хэш. **Исключения:** `OSError`/`UnicodeDecodeError`, ошибки YAML — не перехватываются.

### `iter_markdown_files(vault_path: Path)` (генератор)
`sorted(rglob("*.md"))`, пропуская файлы в скрытых папках (`.obsidian` и др.).

### `read_vault(vault_path: Path) -> list[ParsedNote]`
Парсит все Markdown-файлы. **Исключения:** `FileNotFoundError`, если `vault_path` не существует.

### `read_single_note(vault_path: Path, rel_path: str) -> ParsedNote | None`
Одна заметка; `None`, если файла нет.

Документация по `vault/reader.py` завершена. Далее — `db.md`.
