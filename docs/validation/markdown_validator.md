# Документация: `validation/markdown_validator.py`

> Reference-док. Обзор пакета — `_index.md`.

## Модульные объекты
- `_md = MarkdownIt("commonmark")` — единый переиспользуемый парсер.
- `_CODE_FENCE_RE` — объявлен, но не используется (подсчёт fence идёт через `text.count("```")`).

## `_count_substantial_paragraphs(text: str, min_chars: int = 40) -> int` (приватная)
Парсит текст, считает `inline`-токены сразу после `paragraph_open` с `len(content.strip()) >= min_chars` — «содержательные» абзацы.

## `validate_markdown_body(draft: DraftNote) -> list[ValidationIssue]`

Последовательность:
1. Если `body_md.strip()` пуст И `append_section` пуст → `error`, `empty_body`, **немедленный возврат**.
2. `text = body_md or append_section or ""` (для update ревьюится добавляемый блок).
3. Нечётное число ` ``` ` → `error`, `unbalanced_code_fence`.
4. `_md.parse(text)` в `try/except` → при сбое `error`, `markdown_parse_error`.
5. `len(text) < 40` → `warning`, `very_short_note`; иначе, только для `action == CREATE`, если содержательных абзацев < 3 → `warning`, `note_too_short_structural`.

**Исключения:** не поднимает.

## `validate_headings_coverage(draft: DraftNote, note: OutlineNote | None) -> list[ValidationIssue]`

Проверяет, что все `##`-заголовки плана (`note.subpoints[*].heading`) есть в тексте (`f"## {heading}" in text`). Если `note is None` (план не передан или у объединённого черновика `note_id=""`, `../staging/draft_merge.md`) — `[]`, сознательный no-op. Даёт только `warning` `missing_outline_heading`, по одному на отсутствующий заголовок; approve не блокирует.

Документация по `validation/markdown_validator.py` завершена. Далее — `link_validator.md`.
