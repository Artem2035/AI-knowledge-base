# Документация: `validation/markdown_validator.py`

> Reference-док. Обзор пакета — `_index.md`.

## Модульные объекты

| Имя | Назначение |
|---|---|
| `_md` | `MarkdownIt("commonmark").enable("table")`: таблицы нужны подсчёту блоков, в чистом commonmark их нет. |
| `_CODE_FENCE_RE`, `_INLINE_CODE_RE`, `_URL_RE`, `_MATH_BLOCK_RE`, `_MATH_INLINE_RE` | Вырезают код, URL и формулы перед оценкой языка. |
| `_MIN_CYRILLIC_SHARE = 0.3` | Порог доли кириллицы среди букв прозы. |
| `_MIN_LETTERS_FOR_LANG_CHECK = 200` | Короче текст не проверяется (шум). |

## `_cyrillic_share(text: str) -> tuple[float, int]` (приватная)

Доля кириллицы среди букв **прозы** (после вырезания fenced-кода, формул, inline-кода и URL). Возвращает `(доля, число учтённых букв)`; нет букв → `(1.0, 0)`. Незакрытый fence не вырезается (regex ждёт закрытие), это допущение защитной проверки.

## `_count_content_blocks(text: str, min_chars: int = 40) -> int` (приватная)

Считает **содержательные блоки верхнего уровня**: абзац (≥ `min_chars`), список целиком (один блок), fenced-код, таблицу.

Не считаются: blockquote целиком (служебные callout'ы «Требует проверки», «Проверьте факты», резюме), содержимое списков (список уже учтён), placeholder недостающего раздела (`PLACEHOLDER_MARKDOWN`). Иначе заметка из заглушек и предупреждений проходила бы проверку структуры. Прежний подсчёт «абзацев ≥40 символов» заменён этим: он ложно срабатывал на заметках из кода, списков и таблиц.

## `validate_markdown_body(draft: DraftNote) -> list[ValidationIssue]`

Последовательность:
1. `body_md` и `append_section` пусты → `error` `empty_body`, **немедленный возврат**.
2. `text = body_md or append_section` (для update проверяется добавляемый блок).
3. Нечётное число строк-ограждений (`has_balanced_fences`) → `error` `unbalanced_code_fence`. Тройные кавычки внутри строки не считаются.
4. `_md.parse(text)` в `try/except` → `error` `markdown_parse_error`.
5. `len(text) < 40` → `warning` `very_short_note`. Иначе, только для `action == CREATE` **и не MOC** (`is_moc`), если содержательных блоков < 3 → `warning` `note_too_short_structural` (в сообщении указано число найденных блоков).
6. Проверка языка (всегда, независимо от шага 5): если учтено ≥200 букв и доля кириллицы < 0.3 → `warning` `note_language_mismatch`. Защита от «съезда» заметки на английский после перевода системных инструкций на английский (`../llm/prompts.md §0`).

**Исключения:** не поднимает.

## `validate_headings_coverage(draft: DraftNote, note: OutlineNote | None) -> list[ValidationIssue]`

Проверяет наличие каждого `## {heading}` плана в `body_md` (или `append_section`). `note is None` (план не передан, у объединённого черновика и MOC `note_id=""`) → `[]`, сознательный no-op. Даёт только `warning` `missing_outline_heading`, по одному на отсутствующий заголовок.

**Ограничение:** проверка — вхождение подстроки `"## heading"`, поэтому её удовлетворяет и `### heading`. Для сборки кодом это не проблема (заголовки ставит код), для ручных правок — возможное ложное «ок».

Документация по `validation/markdown_validator.py` завершена. Далее — `link_validator.md`.