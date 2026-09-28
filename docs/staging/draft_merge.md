# Документация: `staging/draft_merge.py` — объединение готовых заметок (без LLM)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Опциональный финальный шаг ПЕРЕД validation/staging
(`settings.enable_draft_merging`, `../config/settings.md §4`).
Принципиально: НИ ОДНОГО вызова LLM. Каждая исходная заметка уже содержит
готовый `body_md` со своими `##`-заголовками; объединение — чисто
текстовая пересборка: (1) оборачивает `body_md` каждой в
`## {исходный title}`; (2) сдвигает внутренние заголовки на 1 уровень
глубже; (3) объединяет `tags`/`links_out`/`source_refs` с дедупликацией.

## 1. `_shift_headings(text: str, levels: int = 1) -> str` (приватная)

Увеличивает уровень каждого Markdown-заголовка на `levels` (`## → ###`,
максимум `######`), НЕ трогая `#`-строки внутри fenced code blocks
(иначе `# comment` в примерах кода сломались бы). Пустой ввод возвращается
как есть. **Исключения:** не поднимает.

## 2. `merge_drafts(drafts, indices, merged_title="", merged_path="") -> DraftNote`

**Описание.** Объединяет несколько готовых `DraftNote` (только
`action=create`) в один. `tags`/`links_out`/`source_refs` — с
дедупликацией по порядку появления. `needs_review`/`critic_rounds` —
`any(...)`/`max(...)` (консервативно: если хоть одна заметка требует
внимания — итоговая тоже). `note_id` результата — пустой (`""`): исходный
план для объединённой структуры не актуален, поэтому
`validate_headings_coverage` для него — no-op
(`../validation/markdown_validator.md`) — сознательный компромисс.

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Полный список черновиков задачи. |
| `indices` | `list[int]` | Индексы для объединения (минимум 2, дедуплицируются и сортируются). |
| `merged_title` | `str` | Иначе `" + ".join(titles)`. |
| `merged_path` | `str` | Иначе `build_note_path(folder, title)` (`../tools/markdown_tools.md`). |

**Возвращаемое значение:** `DraftNote` (`action=CREATE`, `folder` и
`frontmatter` — от первой исходной заметки).

**Исключения (`ValueError`):** меньше 2 уникальных индексов; индекс вне
диапазона; среди выбранных есть `action != CREATE` (у `update` нет
самостоятельного `body_md`, только `append_section`).

## 3. `apply_merges(drafts, merge_groups: list[tuple[list[int], str]]) -> list[DraftNote]`

Применяет НЕСКОЛЬКО непересекающихся групп за один проход. Объединённый
драфт встаёт на место минимального индекса своей группы; заметки вне
групп сохраняют порядок.

**Исключения:** `ValueError` — если группы пересекаются.

## 4. `merge_all_drafts(drafts, merged_title="") -> list[DraftNote]`

Режим `draft_merge_mode="all"`: объединяет ВСЕ `create`-черновики в один.
`update`-черновики не включаются НИ ПРИ КАКОМ режиме. Если `create`
меньше двух — возвращает `drafts` без изменений (не ошибка).

Где вызывается: `cli/draft_merge_editor.py::confirm_merges`
(`../cli/draft_merge_editor.md`) из `Orchestrator.run()` после
`synthesis_done`, до validation, если `enable_draft_merging=True`.

Документация по `staging/draft_merge.py` завершена. Пакет `staging/`
закрыт — обзор `_index.md`.
