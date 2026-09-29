# Документация: `tools/markdown_tools.py` — генерация Markdown (skill Writer)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Чистые детерминированные функции: форматирование готовых данных `DraftNote` (`../storage/models.md §4.2`) в YAML+Markdown, без LLM.

## 1. Константы

| Имя | Назначение |
|---|---|
| `_INVALID_FS_CHARS` | `[\\/:*?"<>\|#^\[\]]` — вырезаются из имён файлов. |
| `_NESTED_WIKILINK_RE` | `\[{2,}([^\[\]]+)\]{2,}` — ловит задублированные скобки (`[[[[X]]]]`). |
| `_DASH_VARIANTS` | Unicode-тире/дефисы → `-` (для сравнения заголовков). |
| `_ALLOWED_FRONTMATTER_KEYS` | `("title", "tags", "created", "source")` — **единственный источник истины** по составу YAML. Контроль в коде, не в промпте: лишние ключи, придуманные LLM, не попадут в файл. `source` — для `RESEARCH_MODE=knowledge` (`model-knowledge`). |

## 2. Функции

### `slugify_filename(title: str) -> str`
Безопасное имя файла, **сохраняя кириллицу**: NFC, `/`/`\` → пробел, вырезание `_INVALID_FS_CHARS`, схлопывание пробелов. Пусто → `"Без названия"`.

### `build_note_path(folder: str, title: str) -> str`
`{folder}/{slug}.md` или `{slug}.md` при пустой папке (`folder.strip("/")`).

### `render_frontmatter(frontmatter: dict) -> str`
YAML-блок только из `_ALLOWED_FRONTMATTER_KEYS` в порядке title/tags/created/source (`sort_keys=False`); пустые значения пропускаются; `created` по умолчанию — сегодня UTC. Возвращает `---\n...---\n`.

### `render_sources_block(source_refs: list[str]) -> str`
`## Источники` со списком URL в НАЧАЛЕ тела (не YAML). Пустой список → `""` (всегда так в knowledge-режиме).

### `render_markdown(draft: DraftNote) -> str`
Собирает файл: frontmatter (`title` и `tags` всегда из `draft`) → блок источников → `sanitize_wikilinks(body_md.strip())` → при непустом `links_out` секция `## Связанные заметки` (каждый элемент через `strip_wikilink_brackets`, обёртка `[[...]]` ровно один раз). Используется `vault/writer.py` и `staging/changeset.py`.

### `insert_wikilinks(body_md: str, titles_to_link: list[str]) -> str`
Проставляет `[[wikilink]]` на ПЕРВОЕ вхождение каждого заголовка (границы слова, lookbehind/lookahead против уже существующих ссылок; длинные заголовки первыми). В активном пайплайне не вызывается — Writer сам отдаёт `links_out`; доступна как skill.

### `sanitize_wikilinks(text: str) -> str`
Схлопывает `[[[[X]]]]` → `[[X]]` до стабилизации. Для ТЕЛА заметки, где ссылка должна остаться ссылкой.

### `strip_wikilink_brackets(text: str) -> str`
Полностью убирает обрамляющие `[[ ]]`. Для мест, где скобки добавляются программно (`links_out`) — иначе LLM-строка `"[[Title]]"` давала баг `[[[[Title]]]]`. Отличие от `sanitize_wikilinks`: та оставляет одну пару, эта — ни одной.

### `normalize_link_title(title: str) -> str`
NFC + унификация тире. ТОЛЬКО для сравнения (`validation/link_validator.py`, `roles/synthesizer_writer.py::_resolve_link`), не для отображения.

Все функции модуля не поднимают исключений.

Документация по `tools/markdown_tools.py` завершена. Далее — `dedup.md`.
