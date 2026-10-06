# Документация: `roles/annotator.py` — роль Annotator

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Один лёгкий батчевый вызов на несколько заметок: модель возвращает **только** теги, `links_out` и `abstract`. Тело заметки собирает код (`../tools/note_assembly.md`), модель его не пишет. Вход модели — детерминированный дайджест (заголовки + начало каждой секции, без кода и таблиц), а не полный текст, поэтому вызов дешёвый по TPM. Вызывается для заметок `action="create"`; для `update` аннотатор не вызывается (`vault/writer.py` дописывает только `append_section`, теги и резюме там не используются).

Клиент: `Orchestrator.self.llm`. Тег роли: `role="annotator"`. Системная инструкция: `llm/prompts/annotator.py::SYSTEM_INSTRUCTION` (`../llm/prompts.md`). Схема ответа: `AnnotationBatchOutput` (`../llm/schemas.md`). Резерв вывода: `groq_reserved_output_by_role["annotator"]` (2000), `reasoning_effort="low"`.

## 1. Константы

| Имя | Значение | Назначение |
|---|---|---|
| `_MAX_NOTES_PER_BATCH` | `6` | Качественный потолок заметок на один вызов (вывод растёт линейно, ~200 токенов на заметку). |
| `_MAX_SPLIT_DEPTH` | `2` | Сколько раз делим батч пополам при `LLMSchemaError`. |
| `_MAX_TAGS` | `6` | Потолок тегов на заметку после нормализации. |
| `_SECTION_DIGEST_CHARS` | `160` | Длина начала секции в дайджесте. |
| `_MIN_SECTION_DIGEST_CHARS` | `40` | Нижняя граница при сжатии дайджеста длинной заметки. |
| `_NOTE_DIGEST_MAX_CHARS` | `1500` | Общий потолок дайджеста заметки. |

## 2. `class AnnotationUnit` (dataclass)

| Поле | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана. |
| `digest` | `str` | Дайджест заметки. |
| `wants_abstract` | `bool` | `True`, если подпунктов ≥ `ABSTRACT_MIN_SECTIONS` (8). |

## 3. Чистый код (без LLM)

### `_truncate(text, limit) -> str` (приватная)
Обрезка по границе слова с `…`.

### `build_digest(note: OutlineNote, sections: list[SectionDraft]) -> str`
Первая строка `Заметка: {title}`, затем по строке на подпункт: `- {heading}: {начало секции}`. Начало берётся через `note_assembly.plain_text` (без кода, таблиц, заголовков, шапок callout'ов, `[[X]]` раскрыто до текста) и обрезается до `max(40, min(160, 1500 // число_подпунктов))` символов. Подпункт без секции — только заголовок. Не поднимает.

### `build_annotation_units(plan, sections, already_done_note_ids) -> list[AnnotationUnit]`
Единицы для заметок с `action == "create"`, чьего `note_id` ещё нет в `already_done_note_ids`.

### `_render(u, index=None) -> str` (приватная)
Текст единицы для промпта: `=== Заметка [i] ===`, дайджест, строка `abstract: required` / `abstract: not needed`.

### `_frame(plan, known_titles) -> str` (приватная)
Статичная рамка промпта: тема и список известных заголовков для `links_out`.

### `normalize_tags(raw_tags: list[str]) -> list[str]`
Формат Obsidian: нижний регистр, без `#`, пробелы → `-`, посторонние символы вырезаются (остаются буквы, цифры, `_`, `-`, `/`), без дублей и чисто числовых тегов, не более `_MAX_TAGS`. Порядок сохраняется.

### `_clean_links(raw_links, own_title, title_map) -> list[str]` (приватная)
Через `markdown_tools.snap_link`; оставляет **только** заголовки из `title_map` (никаких «красных» ссылок от модели), без ссылки на саму заметку и без дублей.

### `_empty(u) -> NoteAnnotation` (приватная)
Пустая аннотация (`note_id` без тегов/ссылок/резюме).

### `_to_annotations(output, units, title_map) -> list[NoteAnnotation]`
Сопоставляет `item.index` с единицами батча. Индекс вне диапазона и дубли отбрасываются (с `logger.warning`); для заметки без ответа — пустая аннотация. `abstract` сохраняется, только если `wants_abstract` (иначе `""`). Возвращает ровно по одной аннотации на единицу, в порядке `units`.

## 4. Вызовы LLM

### `_annotate_batch(units, plan, known_titles, title_map, client, status, depth=0) -> list[NoteAnnotation]` (приватная)
Один `generate_structured(role="annotator", …)`. **Сбой аннотатора не ошибка валидации** (теги, ссылки и резюме не критичны), поэтому:
- `LLMPromptTooLargeError`: одна заметка → пустая аннотация; иначе деление пополам;
- `LLMSchemaError`: при `depth >= 2` или одной заметке → пустые аннотации; иначе деление пополам с `depth + 1`.

Остальные исключения (`LLMFreeLimitReached`, `LLMTaskBudgetExceeded` и др.) пробрасываются до `Orchestrator.run()`.

### `annotate_notes(plan, sections, title_map, client, status, already_done_note_ids, on_batch_done) -> None`
Строит единицы, делит их `llm/chunking.py::batch_for_quality_and_budget` (токен-бюджет + потолок 6 заметок), на каждый батч вызывает `_annotate_batch` и затем `on_batch_done(note_ids, annotations)`. Orchestrator обязан немедленно персистить `note_ids` в чекпоинт (`../staging/checkpoint.md`). Если единиц нет — выход без вызовов.

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Тема и заметки. |
| `sections` | `list[SectionDraft]` | Готовые секции (источник дайджестов). |
| `title_map` | `dict[str, str]` | Из `synthesizer_writer.prepare_linking_context`. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `already_done_note_ids` | `set[str]` | Для resume. |
| `on_batch_done` | `Callable[[list[str], list[NoteAnnotation]], None]` | Колбэк после каждого батча. |

### `annotate_notes_sync(plan, sections, title_map, client, status) -> list[NoteAnnotation]`
Без чекпоинтинга: для тестов и прямых вызовов.

Документация по `roles/annotator.py` завершена. Далее — `synthesizer_writer.md`.