# Документация: `staging/changeset.py` — сохранение/загрузка `StagingChangeset`

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** `StagingChangeset` (`../storage/models.md §5.3`)
Orchestrator строит в самом конце workflow и сохраняет на диск ДО показа
пользователю diff. Ничего не пишет в реальный Vault — файлы лежат ВНЕ
Vault, в `settings.staging_dir`.

## 1. `staging_task_dir(staging_dir: Path, task_id: str) -> Path`

Строит путь к каталогу задачи: `staging_dir / task_id` (может не
существовать). Чистая конкатенация путей, не обращается к ФС.

| Имя | Тип | Назначение |
|---|---|---|
| `staging_dir` | `Path` | Из `settings.staging_dir` (`../config/settings.md §7`). |
| `task_id` | `str` | Идентификатор задачи. |

## 2. `save_changeset(staging_dir: Path, changeset: StagingChangeset) -> Path`

**Описание.** Сохраняет ДВА артефакта:
1. `changeset.json` — машиночитаемый дамп
   (`changeset.model_dump_json(indent=2)`), из которого читает `approve`;
2. `notes_preview/*.md` — человекочитаемое превью каждого черновика (и
   `creates`, и `updates`) для просмотра в обычном редакторе, ВНЕ Vault.

Для превью используется `draft.append_section` (для `update`) или полный
рендер `tools/markdown_tools.py::render_markdown(draft)`
(`../tools/markdown_tools.md`). Имя файла превью — путь черновика с
заменой `/` на `__` (`Знания/X.md` → `Знания__X.md`).

**Возвращаемое значение:** `Path` — каталог задачи (внутри уже лежат
`changeset.json` и `notes_preview/`).

**Исключения:** `OSError` при проблемах записи — не перехватывается.

**Побочные эффекты:** создаёт `task_dir` и `task_dir/"notes_preview"`.

## 3. `load_changeset(staging_dir: Path, task_id: str) -> StagingChangeset | None`

Загружает `changeset.json` обратно в объект. Используется в
`cli/main.py::approve` (`../cli/_index.md`).

**Возвращаемое значение:** `None`, если файла нет.

**Исключения:** `json.JSONDecodeError` (файл повреждён),
`pydantic.ValidationError` (не соответствует схеме) — не перехватываются.

## 4. `list_pending_tasks(staging_dir: Path) -> list[str]`

`task_id` всех задач, ожидающих `approve` — подкаталоги `staging_dir` с
`changeset.json` внутри. Пустой список, если `staging_dir` не существует.
Используется командой `pending`.

Документация по `staging/changeset.py` завершена. Далее — `diff.md`.
