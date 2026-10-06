# Документация: `tools/note_assembly.py` — детерминированная сборка заметок (без LLM)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Заменяет прежнюю цепочку «Evidence → Writer → Critic»: модель пишет только готовый markdown отдельных разделов (`roles/elaborator.py`) и метаданные (`roles/annotator.py`), а тело заметки, MOC и `DraftNote` собирает этот модуль. Ни одного вызова LLM. Вызывается из `Orchestrator.run()` (`../orchestrator/state_machine.md`) заново на каждом запуске (в чекпоинте `DraftNote` не хранятся, `../staging/checkpoint.md`).

## 1. Константы

| Имя | Значение | Назначение |
|---|---|---|
| `ABSTRACT_MIN_SECTIONS` | `8` | Резюме-callout вставляется только в заметки с ≥8 подпунктами. |
| `MIN_HEADING_LEVEL` / `MAX_HEADING_LEVEL` | `2` / `4` | Допустимый диапазон заголовков внутри секции (`##`…`####`). |
| `PLACEHOLDER_MARKDOWN` | `_Раздел не удалось сгенерировать…_` | Текст-заглушка для раздела, который модель не вернула. Не считается содержательным блоком (`../validation/markdown_validator.md`). |
| `HUMANITIES_CALLOUT` | callout `[!warning] Проверьте факты` | Общее предупреждение сверху create-заметки домена `humanities`. |
| `HUMANITIES_UPDATE_CALLOUT` | то же, «в добавленных ниже разделах» | Вариант для `append_section` (update). |
| `MOC_TAG` | `"moc"` | Тег заметки-оглавления. |
| `MOC_DESCRIPTION_MAX_CHARS` | `150` | Потолок описания пункта MOC. |
| `_HOMOGLYPH_MAP` | dict | Латинская буква → кириллический двойник (a, c, e, o, p, x, y, A, B, C, E, H, K, M, O, P, T, X). |
| `_NON_BREAKING_HYPHEN` | `"\u2011"` | Заменяется на `-`. |

## 2. Работа с fenced-кодом и заголовками

### `has_balanced_fences(text: str) -> bool`
Чётное ли число строк-ограждений ` ``` ` (строка начинается с ≤3 пробелов и ` ``` `). Тройные кавычки внутри строки не считаются. Не поднимает.

### `close_unbalanced_fence(text: str) -> str`
Если ограждений нечётное число — дописывает `\n```` в конец. Иначе возвращает текст как есть.

### `normalize_headings(text: str) -> str`
Приводит уровни заголовков **вне** fenced-кода к диапазону `##`…`####` (`#` → `##`, глубже `####` → `####`). Строки внутри кода не трогаются.

### `strip_leading_duplicate_heading(text: str, heading: str) -> str`
Если первая непустая строка — заголовок, совпадающий с `heading` из плана (без учёта регистра), удаляет её: код сам добавляет `## heading`.

## 3. Чистка текста секции

### `fix_text_glitches(text: str) -> str`
Две замены, только вне fenced-кода, блоков `$$ … $$` и защищённых фрагментов (`markdown_tools.PROTECTED_SPAN_RE`: inline-код, формулы, `[[ссылки]]`, markdown-ссылки, URL):
1. **Латинские омоглифы в кириллических словах** (пример: «однoмерная» с латинской `o`). Слово исправляется, только если кириллических букв в нём больше латинских **и** у каждой латинской есть кириллический двойник. Идентификаторы и термины (`numpy`, `DataFrame`, `значениеDF`) остаются как есть.
2. **U+2011 → `-`**.

Проверка синтаксиса кода не выполняется. Не поднимает.

### `prepare_section(markdown: str, heading: str = "") -> tuple[str, bool]`
Порядок: `fix_text_glitches` → `strip_leading_duplicate_heading` (после чистки, чтобы заголовок с омоглифом тоже распознавался как дубль) → `normalize_headings` → `strip`. Возвращает `(текст, fence_сбалансирован)`. Используется `roles/elaborator.py::_accept_sections`. Не поднимает.

## 4. Извлечение простого текста

### `plain_text(markdown: str, *, skip_callouts: bool = False) -> str`
Текст без кода (в т.ч. незакрытого fence), таблиц и заголовков; `[[X]]` и `[[X|алиас]]` раскрываются до текста; `*` и обратные кавычки снимаются. Шапки callout'ов (`> [!…]`) пропускаются всегда. При `skip_callouts=True` пропускается **всё** содержимое blockquote — нужно, чтобы служебные предупреждения сборки не попадали в описания. Переехала из `roles/annotator.py` (там осталось использование для дайджеста с `skip_callouts=False`). Не поднимает.

### `first_sentence(text: str, limit: int = 150) -> str`
Первое предложение: граница — `.`/`!`/`?` + пробел + заглавная буква (поэтому «т.е. значение» не рвётся). Длиннее `limit` — обрезается по границе слова с `…`. Пустой ввод → `""`.

## 5. Сборка заметки

### `_callout(kind, title, text) -> str` (приватная)
`> [!kind] title` + строки текста с префиксом `> `.

### `assemble_note_markdown(note, sections, *, domain, abstract="", for_update=False) -> tuple[str, list[str]]`
Собирает тело: блоки идут в порядке `note.subpoints`.
- `humanities`: сверху `HUMANITIES_CALLOUT` (для update — `HUMANITIES_UPDATE_CALLOUT`);
- резюме `[!abstract] Кратко` — только если `abstract` непуст, подпунктов ≥ `ABSTRACT_MIN_SECTIONS` и не `for_update`;
- каждый подпункт: `## heading`, при `needs_check` callout `[!warning] Требует проверки`, затем текст секции; без секции — `PLACEHOLDER_MARKDOWN` и `needs_check`.

Секции других заметок (`note_id`) игнорируются. **Возвращает** `(markdown, заголовки разделов с needs_check)`. Не поднимает.

### `add_domain_tag(tags: list[str], domain: str) -> list[str]`
Добавляет `domain/<домен>` без дублей.

### `build_draft_note(note, sections, annotation, *, domain, mark_source=None) -> DraftNote`
Путь, action, папку и заголовок решает **план**, а не модель.
- `create`: `body_md`, теги (+ `domain/…`), `links_out`, abstract — из аннотации (`annotation=None` допустим);
- `update`: только `append_section` (теги, ссылки и резюме игнорируются; пустое тело → `None`, это поймает `empty_body`);
- `frontmatter`: `created` (+ `source`, если задан `mark_source`);
- `unverified_sections` — из `assemble_note_markdown`.

**Исключения:** `ValueError` — `action="update"` без `existing_path`.

### `apply_inline_links(draft: DraftNote) -> DraftNote`
Проставляет inline-`[[ссылки]]` по `links_out` (`markdown_tools.insert_wikilinks`: только первое вхождение, без кода/заголовков/таблиц). Не применяется к update, MOC и пустому телу; ссылка на саму себя не ставится. Возвращает тот же объект, если тело не изменилось.

## 6. MOC

### `_moc_description(draft, annotation) -> str` (приватная)
`abstract` аннотации (если непуст), иначе первое предложение из `draft.body_md`: `PLACEHOLDER_MARKDOWN` удаляется, текст берётся через `plain_text(skip_callouts=True)` и `first_sentence`. Работает и для объединённых черновиков (`note_id=""`).

### `build_moc(plan, drafts, annotations_by_note, *, domain, default_folder, existing_paths) -> DraftNote | None`
Оглавление темы без LLM. `None`, если create-заметок (без MOC) меньше двух.
- заголовок `{topic_title} — обзор`; при коллизии пути/заголовка с Vault или другим черновиком — суффикс « (2)», « (3)»…;
- тело: непустой `plan.summary` как `> [!abstract] Кратко` (иначе блок пропускается), затем `## Заметки` со списком `- [[заголовок]] — описание`;
- `links_out` заполнен (для `build_relationships` и `validate_links`), но секцию «Связанные заметки» `render_markdown` для MOC не добавляет;
- `frontmatter.source` **не ставится** (MOC — оглавление, diff не должен помечать его «без внешних источников»);
- теги `moc`, `domain/<домен>`; `is_moc=True`, `note_id=""`.

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | `topic_title`, `summary`. |
| `drafts` | `list[DraftNote]` | Итоговые черновики (после слияния и inline-ссылок). |
| `annotations_by_note` | `dict[str, NoteAnnotation]` | Для описаний (`abstract`). |
| `domain`, `default_folder` | `str` | Тег домена и папка темы. |
| `existing_paths` | `set[str]` | `db.get_all_paths()`, защита от коллизии. |

Документация по `tools/note_assembly.py` завершена. Далее — `markdown_tools.md`.