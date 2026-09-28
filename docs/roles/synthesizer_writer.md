# Документация: `roles/synthesizer_writer.py` — роль Obsidian Writer

> Reference-док. Обзор пакета — `_index.md`. Оркестрируется ролью
> `critic.md` (Writer → Critic → возможно Writer снова).

**Назначение.** Пишет текст ОДНОЙ заметки (Markdown-тело, теги, исходящие
wikilinks) по уже утверждённому плану (заголовок/action/папка —
зафиксированы `Plan`/`Vault Analyst`, Writer их НЕ выбирает). Вызывается
один раз на КАЖДУЮ заметку плана, из `roles/critic.py::run_critic_cycle`
(`critic.md §2`).

## 1. `prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]`

**Описание.** ЧИСТЫЙ КОД. Строит (1) полный отсортированный список
ЗАГОЛОВКОВ всех заметок плана (даже ещё не написанных — их заголовки уже
зафиксированы планом) и (2) карту "нормализованный заголовок → каноническое
написание" (для снаппинга ссылок модели к точному написанию с учётом
Unicode-вариантов дефиса, см. `tools/markdown_tools.py::normalize_link_title`,
`../tools/markdown_tools.md`).

**Возвращаемое значение:** `tuple[list[str], dict[str, str]]` —
`(known_titles отсортированный без дублей, title_map: normalize(title) -> title)`.

**Исключения:** не поднимает.

**Примечание:** список содержит заголовки заметок ИЗ ЭТОГО ПЛАНА —
заголовки существующих заметок Vault (для ссылок на них) в текущей
реализации сюда НЕ подмешиваются этой функцией — это ответственность
вызывающего кода, если потребуется.

## 2. `write_note(note, evidence, known_titles, title_map, client, status, extra_instructions="", mark_source=None, system_instruction=None) -> DraftNote`

**Описание.** Главная функция роли. Фильтрует `evidence` только по
`e.note_id == note.note_id` (факты ДРУГИХ заметок НЕ попадают в промпт),
группирует по `subpoint_id`, строит листинг разделов с фактами (с указанием
`confidence` и, если есть, `critic_note` в виде "ПРОТИВОРЕЧИВО: ..."),
строит листинг известных заголовков для ссылок, при повторном вызове
(после критика) добавляет блок "ЗАМЕЧАНИЯ ПО ПРЕДЫДУЩЕЙ ВЕРСИИ". Делает
один `generate_structured(role="synthesizer_write", ...)`, затем
конвертирует ответ в `DraftNote` через `_to_draft_note(...)` (§3).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана — источник `title`/`action`/`folder`/`existing_path`/`subpoints`. |
| `evidence` | `list[Evidence]` | ВЕСЬ evidence задачи (не только этой заметки) — фильтрация по `note_id` происходит ВНУТРИ функции. |
| `known_titles` | `list[str]` | Из `prepare_linking_context(...)`. |
| `title_map` | `dict[str, str]` | Из `prepare_linking_context(...)` — для снаппинга ссылок в `_to_draft_note`. |
| `client` | `LLMClient` | Обычно `Orchestrator.self.llm`. |
| `status` | `TaskStatus` | Учёт бюджета. |
| `extra_instructions` | `str` (дефолт `""`) | Feedback критика при повторном вызове (`critic.md §1`) — вставляется в промпт как отдельный блок. |
| `mark_source` | `str \| None` (дефолт `None`) | Если задано (напр. `"model-knowledge"`) — прокидывается в `frontmatter["source"]` итогового `DraftNote`. |
| `system_instruction` | `str \| None` (дефолт `None`) | Позволяет вызывающему коду (`Orchestrator`) подставить нестандартный системный промпт (напр. с `MERGE_AWARENESS_GUIDANCE`, `../llm/prompts.md §5`); при `None` используется дефолтный `WRITE_SYSTEM_INSTRUCTION`. |

**Возвращаемое значение:** `DraftNote` (`../storage/models.md §4.2`) — см.
`_to_draft_note` за деталями построения.

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`;
отдельно — см. §3 (`ValueError` при `action="update"` без `existing_path`).

**Тег роли для `GroqClient`:** `role="synthesizer_write"`.

## 3. `_to_draft_note(note: OutlineNote, output: DraftNoteOutput, title_map: dict[str, str], mark_source: str | None = None) -> DraftNote` (приватная)

**Описание.** ЧИСТЫЙ КОД — конвертирует сырой ответ модели
(`DraftNoteOutput`, `../llm/schemas.md §4`) в доменный `DraftNote`, с
ключевым архитектурным правилом: **путь, action, папка и заголовок решает
ПЛАН (Vault Analyst), а НЕ то, что вернула модель на этом шаге** — даже
если `output.action` не совпадает с `note.action`, он игнорируется.

Логика:
- если `note.action == "update"` — путь берётся из `note.existing_path`
  (обязателен, иначе `ValueError`);
- иначе — путь строится через
  `tools/markdown_tools.py::build_note_path(note.folder, note.title)`
  (`../tools/markdown_tools.md`);
- `frontmatter = {"created": сегодняшняя дата UTC}`, плюс
  `frontmatter["source"] = mark_source`, если задан;
- прочие ключи `output.frontmatter_extra` от модели ИГНОРИРУЮТСЯ намеренно
  (состав frontmatter — единая точка правды в коде, не в промпте, см.
  `../tools/markdown_tools.md`);
- каждая ссылка из `output.links_out` прогоняется через внутреннюю
  `_resolve_link(raw)`: снимает обрамляющие `[[...]]`
  (`strip_wikilink_brackets`), отбрасывает URL-подобные значения (по
  ошибке модели, с предупреждением в лог), снаппит к канонической форме
  через `title_map.get(normalize_link_title(link), link)` (если заголовка
  нет в карте — оставляет как есть, т.е. допускает ссылку на заголовок вне
  списка).

**Возвращаемое значение:** `DraftNote` с полями `note_id`, `action`, `path`,
`title`, `folder`, `frontmatter`, `body_md` (через `sanitize_wikilinks`),
`tags`, `links_out` (только успешно резолвленные), `append_section` (через
`sanitize_wikilinks`, `None` если пусто).

**Исключения:** `ValueError` — если `note.action == "update"` и
`note.existing_path` пуст.

## 4. `build_relationships(drafts: list[DraftNote]) -> list[Relationship]`

**Описание.** ЧИСТЫЙ КОД, без LLM. Строит карту "заголовок → путь" по уже
написанным черновикам, затем для каждой исходящей ссылки каждого черновика
создаёт `Relationship(from_note=path, to_note=..., link_type="wikilink")`
(`../storage/models.md §4.3`). Если целевой заголовок не найден в карте
(ссылка на заметку, не входящую в этот batch черновиков — например, на
существующую заметку Vault или красную ссылку) — `to_note` остаётся самим
заголовком as-is (fallback).

**Возвращаемое значение:** `list[Relationship]` — по одной записи на
каждую пару (черновик, исходящая ссылка).

**Исключения:** не поднимает.

Документация по `roles/synthesizer_writer.py` завершена. Обзор пакета —
`_index.md`. Оркестрация bounded-retry с критиком — `critic.md`.
