# Документация: `tools/markdown_tools.py` — генерация и правка Markdown (skill)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Чистые детерминированные функции форматирования данных `DraftNote` (`../storage/models.md`) в YAML+Markdown и работы со ссылками. Без LLM.

## 1. Константы

| Имя | Назначение |
|---|---|
| `_INVALID_FS_CHARS` | `[\\/:*?"<>\|#^\[\]]` — вырезаются из имён файлов. |
| `_NESTED_WIKILINK_RE` | Ловит задублированные скобки (`[[[[X]]]]`). |
| `_DASH_VARIANTS` | Unicode-тире/дефисы → `-` (для сравнения заголовков). |
| `_ALLOWED_FRONTMATTER_KEYS` | `("title", "tags", "created", "source")` — единственный источник истины по составу YAML (контроль в коде, не в промпте). `source` ставится у заметок конспекта по знаниям модели (`model-knowledge`), у MOC не ставится. |
| `_FENCE_LINE_RE` | Строка-ограждение ` ``` `. |
| `_NO_LINK_LINE_RE` | Строки, где ссылки не ставятся: заголовки, строки таблиц, шапки callout'ов. |
| `PROTECTED_SPAN_RE` | **Публичный** (раньше `_PROTECTED_SPAN_RE`). Фрагменты внутри строки, которые не трогаем: inline-код, формулы `$…$`/`$$…$$`, `[[ссылки]]`, markdown-ссылки, URL. Одна захватывающая группа: `re.split` даёт чередование «текст / защищённый фрагмент». Используется `insert_wikilinks` и `note_assembly.fix_text_glitches`. |

## 2. Имена файлов и пути

### `slugify_filename(title: str) -> str`
Безопасное имя файла **с сохранением кириллицы**: NFC, `/` и `\` → пробел, вырезание `_INVALID_FS_CHARS`, схлопывание пробелов. Пусто → `"Без названия"`.

### `build_note_path(folder: str, title: str) -> str`
`{folder}/{slug}.md` или `{slug}.md` при пустой папке (`folder.strip("/")`).

## 3. Рендер файла

### `render_frontmatter(frontmatter: dict) -> str`
YAML только из `_ALLOWED_FRONTMATTER_KEYS` в порядке title/tags/created/source (`sort_keys=False`); пустые значения пропускаются; `created` по умолчанию — сегодня UTC. Возвращает `---\n…---\n`.

### `render_sources_block(source_refs: list[str]) -> str`
Блок `## Источники` в начале тела (не YAML). Пустой список → `""` (в knowledge-режиме всегда пуст).

### `render_markdown(draft: DraftNote) -> str`
Frontmatter (`title`, `tags` всегда из `draft`) → блок источников → `sanitize_wikilinks(body_md.strip())` → при непустом `links_out` **и `not draft.is_moc`** секция `## Связанные заметки` (каждый элемент через `strip_wikilink_brackets`, обёртка `[[…]]` ровно один раз). Для MOC секция не добавляется: список заметок уже в теле. Используется `vault/writer.py` и `staging/changeset.py`.

## 4. Ссылки

### `insert_wikilinks(body_md: str, titles_to_link: list[str]) -> str`
Проставляет `[[wikilink]]` на **первое** вхождение каждого заголовка (точное совпадение, границы слова, без падежей). Не трогает: fenced-код (в т.ч. незакрытый, до конца текста), блоки `$$`, inline-код, формулы, строки заголовков, таблиц и шапок callout'ов, URL, markdown-ссылки, существующие `[[ссылки]]` (в т.ч. `[[Заголовок|алиас]]`, повторно на заголовок ссылка не ставится). Длинные заголовки первыми. Идемпотентна. Вызывается из `note_assembly.apply_inline_links`.

### `sanitize_wikilinks(text: str) -> str`
Схлопывает `[[[[X]]]]` → `[[X]]` до стабилизации. Для тела заметки.

### `strip_wikilink_brackets(text: str) -> str`
Убирает обрамляющие `[[ ]]` полностью. Для мест, где скобки добавляются программно (`links_out`); иначе строка `"[[Title]]"` от модели давала `[[[[Title]]]]`.

### `normalize_link_title(title: str) -> str`
NFC + унификация тире. Только для **сравнения** (`validation/link_validator.py`, `roles/annotator.py`, `staging/draft_merge.py`, `roles/synthesizer_writer.py`), не для отображения.

### `snap_link(raw: str, title_map: dict[str, str]) -> str | None`
Приводит ссылку от модели к каноническому заголовку: снимает `[[ ]]`, отбрасывает URL-подобные значения (`None`, с `logger.warning`), снаппит через `title_map` (`normalize_link_title(title) → title`). Заголовок вне карты возвращается как есть: допустимость ссылки решает вызывающий код (`annotator._clean_links` оставляет только заголовки из карты). `None` — ссылку нужно выбросить.

Функции модуля исключений не поднимают.

Документация по `tools/markdown_tools.py` завершена. Далее — `dedup.md`.