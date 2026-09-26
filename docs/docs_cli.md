# Документация: папка `cli/`

> Единственный слой представления в MVP — Typer-CLI поверх `Orchestrator`
> (`orchestrator/state_machine.py`). Ничего из этой папки не содержит
> бизнес-логики workflow — только консольный ввод/вывод, интерактивные
> callback'и (`plan_confirm_cb`, `merge_confirm_cb`) и финальную команду
> `approve`, которая единственная во всей CLI фактически пишет в реальный
> Vault (через `staging/commit.py::commit_changeset`).

---

## 0. `cli/__init__.py`

Пустой файл-маркер пакета.

---

## 1. `cli/main.py` — точка входа Typer-приложения

**Назначение.** Определяет команды `ask`, `resume`, `resumable`, `approve`,
`pending`, `index`. Общая обвязка запуска/отчёта вынесена в `_run_and_report`
(используется и `ask`, и `resume`, чтобы поведение не расходилось).

### 1.1. `app: typer.Typer`

Корневой объект приложения (`add_completion=False`). `console: rich.console.Console` —
единая точка вывода в консоль для всех команд.

### 1.2. `_run_and_report(orch, *, raw_query, resume_task_id, settings) -> None`

**Описание.** Общая логика запуска (новая задача или resume) + единый вывод
результата. Определяет ТРИ локальных callback'а и передаёт их в `orch.run(...)`:

- `progress_cb(stage: str)` — печатает `f"→ {stage}"` в консоль (dim-стиль).
- `confirm_with_paused_spinner(plan) -> bool` — останавливает spinner
  (`status_ctx.stop()`), вызывает `cli/plan_editor.py::confirm_plan(plan)`,
  возвращает результат, затем ВСЕГДА (в `finally`) перезапускает spinner
  (`status_ctx.start()`) — это и есть `plan_confirm_cb`.
- `merge_confirm_with_paused_spinner(drafts)` — аналогично для
  `cli/draft_merge_editor.py::confirm_merges(drafts, mode=settings.draft_merge_mode)`
  — это `merge_confirm_cb`.

После `orch.run(...)` (в `finally` вызывается `orch.close()`):
- если `result.stopped` — печатает `Panel` с сообщением остановки (оранжевый
  стиль), сколько вызовов потрачено за сессию (`result.status.llm_calls_used`
  из `settings.max_llm_calls_per_task`), завершает процесс `typer.Exit(code=2)`.
- иначе печатает diff (`staging/diff.py::render_diff_summary(result.changeset)`,
  зелёный `Panel`); если `changeset.validation` есть и `not .ok` — печатает
  ошибку и `typer.Exit(code=1)` (approve заблокирован); иначе печатает путь к
  staging-каталогу задачи и подсказку команды `approve <task_id>`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `orch` | `Orchestrator` | Уже созданный экземпляр. |
| `raw_query` | `str \| None` | Новый запрос (для `ask`), либо `None` (для `resume`). |
| `resume_task_id` | `str \| None` | `task_id` для продолжения (для `resume`), либо `None` (для `ask`). |
| `settings` | `Settings` | Для вывода лимитов/режима объединения черновиков в консоль. |

**Возвращаемое значение:** `None` (может завершить процесс через `typer.Exit`).

**Исключения:** не перехватывает исключения `orch.run(...)` напрямую — только
гарантирует `orch.close()` через `finally`. `OrchestratorStopped` из `resume`
перехватывается ВЫШЕ, в самой команде `resume` (см. §1.4), а не здесь.

### 1.3. `ask(query: str = typer.Argument(...)) -> None`

**Описание.** Команда `python -m cli.main ask "..."`. Создаёт `Settings`
(`config.settings.get_settings()`), создаёт `Orchestrator(settings)`, печатает
заголовочную `Panel` с текстом запроса, вызывает `_run_and_report(orch, raw_query=query, resume_task_id=None, settings=settings)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `query` | `str` (обязательный CLI-аргумент) | Запрос пользователя на естественном языке. |

**Возвращаемое значение:** `None`.
**Исключения:** не перехватывает — падения `Orchestrator.__init__`/`run` всплывают как traceback Typer.

### 1.4. `resume(task_id: str = typer.Argument(...)) -> None`

**Описание.** Команда для продолжения остановленной задачи. Создаёт `Settings`
и `Orchestrator`, печатает заголовочную `Panel`, вызывает `_run_and_report(orch, raw_query=None, resume_task_id=task_id, settings=settings)`
внутри `try/except OrchestratorStopped` — при перехвате печатает `Panel` с
ошибкой (красный стиль) и завершает `typer.Exit(code=1)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `task_id` | `str` (обязательный CLI-аргумент) | `task_id` из списка `resumable`. |

**Возвращаемое значение:** `None`.
**Исключения:** `OrchestratorStopped` перехвачена внутри самой команды (в отличие от `ask`).

### 1.5. `resumable() -> None`

**Описание.** Не использует LLM. Показывает список задач с незавершённым
чекпоинтом (`staging/checkpoint.py::list_resumable_tasks`). Для каждой печатает
`task_id`, `last_completed_stage`, обрезанный до 60 символов `raw_query`, время
последнего обновления в московской таймзоне (`ZoneInfo("Europe/Moscow")`,
формат `"%d.%m.%Y %H:%M по мск"`), и `total_llm_calls_used`. Если список пуст —
печатает "Нет задач, ожидающих продолжения." и выходит.

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 1.6. `approve(task_id: str) -> None`

**Описание.** ЕДИНСТВЕННАЯ команда во всей системе, которая реально изменяет
файлы реального Vault. Последовательность:
1. Загружает `changeset` (`staging/changeset.py::load_changeset`) — если
   `None`, печатает ошибку и `typer.Exit(code=1)`.
2. Печатает diff (`render_diff_summary`, оранжевый `Panel`, заголовок
   "Изменения к применению").
3. Если `changeset.validation` есть и `not .ok` — блокирует, `typer.Exit(code=1)`.
4. Запрашивает явное подтверждение `typer.confirm("Применить эти изменения к
   реальному Vault?", default=False)` — если пользователь отказался, печатает
   "Отменено." и `typer.Exit(code=0)` (мягкий выход, не ошибка).
5. `settings.ensure_dirs()`.
6. Создаёт `VaultDB(settings.db_path)`, `try_create_embedder(...)`, вычисляет
   `backup_dir = staging_task_dir(settings.staging_dir, task_id) / "backup_before_update"`.
7. Вызывает `staging/commit.py::commit_changeset(...)` внутри `try/finally`
   (`finally` закрывает `db.close()`, если `db` был создан).
8. Печатает результат: количество изменённых файлов и построчный список
   `{action}: {path}` для каждого `WriteResult`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | `task_id` задачи из staging (`pending`). |

**Возвращаемое значение:** `None`.

**Исключения:** не перехватывает исключения `commit_changeset` напрямую (кроме
гарантированного закрытия `db` в `finally`) — `CommitError`/`FileExistsError`/
`FileNotFoundError`/`PermissionError` из `staging/commit.py` всплывают как
traceback Typer.

### 1.7. `pending() -> None`

**Описание.** Не использует LLM. Печатает список `task_id` задач, ожидающих
`approve` (`staging/changeset.py::list_pending_tasks`). Если список пуст —
"Нет задач, ожидающих подтверждения.".

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 1.8. `index() -> None`

**Описание.** Не использует LLM. Просто пересканирует Vault и обновляет
локальный индекс. `settings.ensure_dirs()`, создаёт `VaultDB`, эмбеддер,
`VaultIndexer(db, vault_path, embedder).sync()` + `.resolve_wikilink_targets()`,
закрывает `db`, печатает статистику (`dict` со `scanned`/`updated`/`unchanged`/`removed`).

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает (может уронить исключение `read_vault`, если
`vault_path` не существует — `FileNotFoundError`, не перехвачена явно).

---

## 2. `cli/plan_editor.py` — интерактивное редактирование `Plan`

**Назначение.** Отображение и редактирование `Plan` (outline) в консоли —
вызывается из `cli/main.py` как `plan_confirm_cb`, сразу после
`outline_planner.build_plan()`, ДО самого дорогого по LLM-бюджету этапа
elaboration. Мутирует переданный `Plan` НАПРЯМУЮ (`add`/`remove`/`rename` на
`plan.notes` и `note.subpoints`) — `Orchestrator` сохраняет уже изменённый
объект в `checkpoint.plan` при `persist("plan_approved")`, отдельной
синхронизации не требуется.

### 2.1. `build_plan_tree(plan: Plan) -> Tree`

**Описание.** Строит `rich.tree.Tree` для визуализации плана: корень —
`plan.summary` (или `plan.topic_title`, если summary пуст), ветви первого
уровня — заметки (`f"{i}. {note.title}"`), листья — подпункты
(`f"{sp.heading}: {sp.covers}"`).

**Параметры:** `plan: Plan`.
**Возвращаемое значение:** `Tree` (объект `rich`, готовый для `console.print`).
**Исключения:** не поднимает.

### 2.2. `_select_note_index(plan: Plan, prompt: str) -> int | None` (приватная)

**Описание.** Запрашивает у пользователя номер заметки (1-based) через
`typer.prompt`, валидирует диапазон.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `plan` | `Plan` | Источник `plan.notes` для диапазона. |
| `prompt` | `str` | Текст приглашения. |

**Возвращаемое значение:** `int | None` — 0-based индекс, либо `None` при
пустом `plan.notes` или некорректном номере (с сообщением об ошибке в консоль).

**Исключения:** не поднимает (некорректный ввод обрабатывается через возврат `None`, не через исключение).

### 2.3. `_parse_indices(raw: str, count: int) -> list[int] | None` (приватная)

**Описание.** Разбирает строку вида `"2,4,5"` (1-based номера) в список
0-based индексов, ОТСОРТИРОВАННЫЙ ПО УБЫВАНИЮ и без дублей — такой порядок
позволяет удалять элементы последовательными `pop()` без пересчёта индексов
оставшихся элементов после каждого удаления.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw` | `str` | Введённая пользователем строка. |
| `count` | `int` | Верхняя граница диапазона (для валидации `0 <= i < count`). |

**Возвращаемое значение:** `list[int] | None` — `None` при: `ValueError` парсинга
(не числа/не запятые), пустом списке индексов, индексе вне диапазона `[1, count]`
(во всех случаях печатается сообщение об ошибке в консоль).

**Исключения:** не поднимает наружу — внутренний `ValueError` перехвачен.

### 2.4. `_select_subpoint_index(note: OutlineNote) -> int | None` (приватная)

**Описание.** Аналог `_select_note_index`, но для подпунктов ОДНОЙ заметки —
печатает нумерованный список подпунктов перед запросом номера.

**Параметры:** `note: OutlineNote`.
**Возвращаемое значение:** `int | None` — 0-based индекс подпункта, либо `None`.
**Исключения:** не поднимает.

### 2.5. `_add_note(plan: Plan) -> None` (приватная)

**Описание.** Запрашивает заголовок новой заметки; если пуст — отмена с
сообщением. Затем в цикле (`typer.confirm("Добавить подпункт к этой заметке?", default=True)`)
запрашивает заголовки/техзадания подпунктов, добавляет их в новый `OutlineNote`.
В конце добавляет заметку в `plan.notes`.

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.6. `_remove_note(plan: Plan) -> None` (приватная)

**Описание.** Запрашивает номера заметок через запятую, парсит через
`_parse_indices` (уже в порядке убывания), удаляет через `plan.notes.pop(i)`
по каждому индексу. Печатает подтверждение с названиями удалённых заметок
(в исходном порядке для вывода — `reversed(removed_titles)`, т.к. собирались
в порядке убывания индексов).

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.7. `_rename_note(plan: Plan) -> None` (приватная)

**Описание.** Выбирает заметку через `_select_note_index`, запрашивает новый
заголовок (дефолт — текущий), если непустой — присваивает `plan.notes[idx].title`.

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.8. `_add_subpoint(plan: Plan) -> None` (приватная)

**Описание.** Выбирает заметку, запрашивает заголовок и техзадание нового
подпункта, добавляет `OutlineSubpoint` в её `subpoints`, если заголовок непуст.

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.9. `_remove_subpoint(plan: Plan) -> None` (приватная)

**Описание.** Выбирает заметку, печатает список её подпунктов, запрашивает
номера через запятую, парсит через `_parse_indices`, удаляет по индексам
(`note.subpoints.pop(i)`), печатает подтверждение.

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.10. `_edit_subpoint(plan: Plan) -> None` (приватная)

**Описание.** Выбирает заметку, затем подпункт внутри неё
(`_select_subpoint_index`), запрашивает новые `heading`/`covers` (дефолты —
текущие значения), присваивает, если непусто.

**Параметры:** `plan: Plan` (мутируется).
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

### 2.11. `_EDIT_MENU`, `_EDIT_ACTIONS`

**Описание.** Статичные структуры меню: `_EDIT_MENU` — список пар `(ключ,
подпись)` для отображения; `_EDIT_ACTIONS` — словарь `ключ → функция-обработчик`
(не включает пункты `"1"`/`"2"`, которые обрабатываются напрямую в `confirm_plan`).

### 2.12. `confirm_plan(plan: Plan) -> bool`

**Описание.** Главная интерактивная функция — показывает план
(`plan.topic_title`, `build_plan_tree`, счётчик заметок/подпунктов), меню
действий, в цикле обрабатывает выбор пользователя:
- `"1"` (утвердить) — если `plan.notes` пуст, печатает ошибку и продолжает
  цикл; иначе возвращает `True`.
- `"2"` (отменить задачу) — возвращает `False`.
- иначе — ищет обработчик в `_EDIT_ACTIONS`, если не найден — печатает "Неизвестное
  действие." и продолжает цикл; если найден — вызывает его (мутирует `plan`)
  и снова показывает план (цикл `while True`).

**Параметры:** `plan: Plan` — мутируется на месте всеми обработчиками.

**Возвращаемое значение:** `bool` — `True`, если план утверждён (действие
`"1"` с непустым `plan.notes`); `False`, если пользователь выбрал отмену
(действие `"2"`).

**Исключения:** не поднимает намеренно (весь пользовательский ввод
обрабатывается с валидацией и повторным приглашением, а не исключениями).

---

## 3. `cli/draft_merge_editor.py` — экран объединения готовых заметок

**Назначение.** Вызывается как `merge_confirm_cb` из `Orchestrator.run()`
ПОСЛЕ Writer+Critic (`synthesis_done`), ДО validation/staging. Активен только
при `settings.enable_draft_merging=True`.

### 3.1. `confirm_merges(drafts: list[DraftNote], mode: str = "select") -> list[DraftNote]`

**Описание.** Два режима поведения по `mode`:

**`mode == "all"`:**
- Если `create_count < 2` (заметок с `action="create"` меньше двух) —
  печатает "Меньше двух новых заметок — объединять нечего." и возвращает
  `drafts` без изменений.
- Иначе печатает сообщение о предстоящем объединении всех `create_count`
  заметок, запрашивает заголовок объединённой заметки (`typer.prompt(...,
  default="")` — пустой ввод означает "составить автоматически"), вызывает
  `staging/draft_merge.py::merge_all_drafts(drafts, merged_title=title)`.

**`mode == "select"` (прежнее поведение по умолчанию):**
- Печатает нумерованный список всех `drafts` с пометкой `" (update — не
  участвует)"` у черновиков с `action != "create"`.
- В цикле `while typer.confirm("Объединить несколько заметок в одну?",
  default=False)`: запрашивает номера через запятую, парсит вручную
  (`int(x.strip()) - 1`, с обработкой `ValueError`), валидирует: минимум 2
  уникальных номера, все в диапазоне, ни один индекс уже не задействован в
  другой группе этого же вызова (`used: set[int]`), все выбранные — именно
  `action == "create"`. При успешной валидации — запрашивает заголовок
  (дефолт — `" + ".join(...)` заголовков), добавляет `(indices, title)` в
  список `groups`, помечает индексы как `used`.
- После выхода из цикла — если `groups` непуст, вызывает
  `staging/draft_merge.py::apply_merges(drafts, groups)`; иначе возвращает
  `drafts` без изменений.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Все черновики задачи (и `create`, и `update`) после Writer+Critic. |
| `mode` | `str` (дефолт `"select"`) | `"all"` или `"select"`. Из `settings.draft_merge_mode`. |

**Возвращаемое значение:** `list[DraftNote]` — либо исходный `drafts` (если
объединять было нечего/пользователь отказался), либо результат
`merge_all_drafts`/`apply_merges`.

**Исключения:** не поднимает намеренно — весь пользовательский ввод в
режиме `"select"` валидируется с повторным приглашением при ошибке, а не
исключениями. Однако `apply_merges`/`merge_all_drafts`/`merge_drafts`
теоретически МОГУТ поднять `ValueError` при неожиданном рассинхроне данных
(см. `docs_staging.md §5`) — в этой функции такой `ValueError` НЕ перехватывается.

---

## Сводная схема: кто кого вызывает в `cli/`

```
cli/main.py::ask/resume
    │
    ▼
cli/main.py::_run_and_report
    │  progress_cb ──────────────► печать в консоль
    │  plan_confirm_cb ──────────► cli/plan_editor.py::confirm_plan(plan)
    │  merge_confirm_cb ─────────► cli/draft_merge_editor.py::confirm_merges(drafts, mode)
    │
    ▼
Orchestrator.run(...)  (см. docs_llm_cycle_part1_orchestrator.md)

cli/main.py::approve(task_id)
    │
    ▼
staging/changeset.py::load_changeset → staging/diff.py::render_diff_summary
    │  typer.confirm(...)
    ▼
staging/commit.py::commit_changeset → vault/writer.py::VaultWriter
```

Документация по `cli/` завершена.
