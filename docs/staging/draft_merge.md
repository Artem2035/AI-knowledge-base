# Документация: `staging/draft_merge.py` — объединение готовых заметок (без LLM)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Опциональный шаг после сборки `DraftNote` и до валидации (`settings.enable_draft_merging`, дефолт в коде `False`). **Ни одного вызова LLM.** Каждая исходная заметка уже содержит готовый `body_md` со своими `##`-заголовками; слияние — чисто текстовая пересборка.

**Место в `Orchestrator.run()`:** `build_draft_note` → `merge_confirm_cb(drafts)` (только если слияние включено и колбэк передан) → **`fix_links_after_merge`** → `apply_inline_links` → `build_moc` → `build_relationships`. Если после слияния осталась одна create-заметка, `build_moc` вернёт `None` (меньше двух).

## 1. `_shift_headings(text: str, levels: int = 1) -> str` (приватная)

Увеличивает уровень каждого Markdown-заголовка на `levels` (`##` → `###`, потолок `######`), не трогая строки внутри fenced-кода. Разбор построчный: **незакрытый fence считается кодом до конца текста**, поэтому `# comment` в нём не станет заголовком. Пустой ввод возвращается как есть.

## 2. `_strip_humanities_callout(text: str) -> tuple[str, bool]` (приватная)

Вырезает общий `HUMANITIES_CALLOUT` (`../tools/note_assembly.md`) из начала тела. Возвращает `(текст, был_ли_callout)`.

## 3. `merge_drafts(drafts, indices, merged_title="", merged_path="") -> DraftNote`

Объединяет несколько create-черновиков в один:
- тело каждого оборачивается в `## {title}`, внутренние заголовки сдвигаются на уровень глубже;
- **висящий fence закрывается до склейки** (`close_unbalanced_fence`), иначе он поглотил бы следующие секции;
- общий humanities-callout вырезается из исходных тел и ставится **один раз сверху**;
- резюме (`[!abstract]`) каждой исходной заметки остаётся под её собственным `## {title}`, общее резюме не создаётся;
- `tags`, `source_refs`, `unverified_sections` объединяются с дедупликацией по порядку появления;
- `links_out`: ссылки на заметки, вошедшие в объединение, и самоссылки убираются (после слияния они указывали бы на несуществующие заголовки);
- `merged_from` — заголовки исходных заметок (если у источника уже есть `merged_from`, берётся он);
- `needs_review` — `any(...)`, `critic_rounds` — `max(...)` (legacy-поля);
- `folder` и `frontmatter` — от первой исходной заметки; путь — `merged_path` или `build_note_path(folder, title)`; заголовок — `merged_title` или `" + ".join(titles)`;
- **`note_id` пуст**: исходный план для объединённой структуры не актуален, `validate_headings_coverage` для неё — no-op.

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Полный список черновиков задачи. |
| `indices` | `list[int]` | Индексы для объединения (дедуплицируются и сортируются). |
| `merged_title`, `merged_path` | `str` | Необязательные. |

**Исключения (`ValueError`):** меньше 2 уникальных индексов; индекс вне диапазона; среди выбранных есть `action != create` (у update нет самостоятельного `body_md`).

## 4. `fix_links_after_merge(drafts: list[DraftNote]) -> list[DraftNote]`

Отдельный проход **после** `apply_merges`/`merge_all_drafts`: заметка вне группы слияния может ссылаться на заголовок, который теперь живёт в чужой объединённой заметке. Строится карта «старый заголовок (нормализованный) → заголовок объединённой заметки» по `merged_from`; ссылки перенаправляются, самоссылки и дубли удаляются. Если слияний не было (ни у кого нет `merged_from`), возвращает `drafts` без изменений. Не поднимает.

## 5. `apply_merges(drafts, merge_groups: list[tuple[list[int], str]]) -> list[DraftNote]`

Применяет несколько непересекающихся групп за один проход. Объединённый черновик встаёт на место минимального индекса своей группы; остальные сохраняют порядок. **Исключения:** `ValueError`, если группы пересекаются.

## 6. `merge_all_drafts(drafts, merged_title="") -> list[DraftNote]`

Режим `draft_merge_mode="all"`: объединяет все create-черновики в один. Update-черновики не включаются ни при каком режиме. Если create меньше двух, возвращает `drafts` без изменений (не ошибка).

Вызывается из `cli/draft_merge_editor.py::confirm_merges` (`../cli/draft_merge_editor.md`).

Документация по `staging/draft_merge.py` завершена. Пакет `staging/` закрыт, обзор — `_index.md`.