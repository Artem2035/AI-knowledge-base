# Документация: папка `staging/`

> Продолжение `docs_storage_models.md`. Здесь описаны все 5 файлов
> `staging/`: `changeset.py`, `checkpoint.py`, `commit.py`, `diff.py`,
> `draft_merge.py`. Это слой между "LLM закончил работу" и "изменения
> попали в реальный Vault" — весь код здесь ЧИСТЫЙ (без LLM), детерминированный.

---

## 0. `staging/__init__.py`

Пустой файл-маркер пакета. Ничего не экспортирует.

---

## 1. `staging/changeset.py` — сохранение/загрузка `StagingChangeset`

**Назначение.** `StagingChangeset` (`storage/models.py §6.3`) — это то, что
`Orchestrator.run()` строит в самом конце workflow и сохраняет на диск ДО
показа пользователю diff. Ничего не пишет в реальный Vault — файлы здесь
лежат ВНЕ Vault, в `settings.staging_dir`.

### 1.1. `staging_task_dir(staging_dir: Path, task_id: str) -> Path`

**Описание.** Строит путь к каталогу конкретной задачи внутри staging-директории.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `staging_dir` | `Path` | Корневая staging-директория. Из `settings.staging_dir`. |
| `task_id` | `str` | Идентификатор задачи. |

**Возвращаемое значение:** `Path` — `staging_dir / task_id` (директория может не существовать).

**Исключения:** не поднимает (чистая конкатенация путей, без обращения к файловой системе).

### 1.2. `save_changeset(staging_dir: Path, changeset: StagingChangeset) -> Path`

**Описание.** Сохраняет ДВА артефакта задачи:
1. `changeset.json` — машиночитаемый полный дамп `StagingChangeset`
   (`changeset.model_dump_json(indent=2)`), из которого позже читается
   `approve`;
2. `notes_preview/*.md` — человекочитаемое превью каждого черновика (и
   `creates`, и `updates`), чтобы пользователь мог открыть файл в обычном
   текстовом редакторе/файловой системе, ВНЕ Vault, до `approve`.

Для превью используется `draft.append_section` (если это `update` с
конкретным добавляемым блоком) или полный рендер `tools/markdown_tools.py::render_markdown(draft)`.
Имя файла превью — путь черновика с заменой `/` на `__` (`Знания/X.md` →
`Знания__X.md`), чтобы избежать создания вложенных папок внутри `notes_preview/`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `staging_dir` | `Path` | Корневая staging-директория. |
| `changeset` | `StagingChangeset` | Что сохранить (обычно уже с заполненным `.validation`). |

**Возвращаемое значение:** `Path` — путь к каталогу задачи (`staging_task_dir(...)`),
внутри которого уже лежат `changeset.json` и `notes_preview/`.

**Исключения:** файловые (`OSError` и подклассы) при проблемах записи на диск —
не перехватываются намеренно (нештатная ситуация, которую лучше не глотать молча).

**Побочные эффекты:** создаёт `task_dir` и `task_dir / "notes_preview"` (`mkdir(parents=True, exist_ok=True)`).

### 1.3. `load_changeset(staging_dir: Path, task_id: str) -> StagingChangeset | None`

**Описание.** Загружает ранее сохранённый `changeset.json` обратно в объект
`StagingChangeset`. Используется в `cli/main.py::approve`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `staging_dir` | `Path` | Корневая staging-директория. |
| `task_id` | `str` | Идентификатор задачи. |

**Возвращаемое значение:** `StagingChangeset | None` — `None`, если файл
`changeset.json` не существует (задача не найдена в staging).

**Исключения:**
- `json.JSONDecodeError` — если файл повреждён (не перехватывается здесь).
- `pydantic.ValidationError` — если содержимое не соответствует схеме
  `StagingChangeset` (не перехватывается здесь).

### 1.4. `list_pending_tasks(staging_dir: Path) -> list[str]`

**Описание.** Список `task_id` всех задач, ожидающих `approve` — используется
командой `cli/main.py::pending`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `staging_dir` | `Path` | Корневая staging-директория. |

**Возвращаемое значение:** `list[str]` — имена подкаталогов `staging_dir`,
у которых внутри есть `changeset.json`. Пустой список, если `staging_dir` не существует.

**Исключения:** не поднимает.

---

## 2. `staging/checkpoint.py` — чекпоинты задач для resume

**Назначение.** Принципиально ОТДЕЛЬНЫЙ механизм персистентности от
`StagingChangeset`: `StagingChangeset` создаётся только на ПОСЛЕДНЕМ шаге
workflow (`"staged"`), а `TaskCheckpoint` перезаписывается ПОСЛЕ КАЖДОГО
завершённого шага, начиная с самого первого — это то, что делает `resume`
возможным, если задача остановилась на исчерпании `MAX_LLM_CALLS_PER_TASK`
задолго до staging (например, посреди `elaborating`, самого "дорогого" по
числу вызовов этапа).

Инвариант: чекпоинт хранит УЖЕ ПРОВАЛИДИРОВАННЫЕ структурированные данные
(`Plan`, `SourceCandidate[]`, `Evidence[]`, ...) — те же Pydantic-модели, что
летают между `roles/*`, поэтому сериализация/десериализация тривиальна и не
завязана на конкретный LLM-провайдер.

**Гранулярность resume по этапам:**
- Planning / Vault-analysis / Synthesis-план (высокоуровнево) — шаг целиком
  "сделан" или "не сделан" (флаги `*_done`).
- Extracting (`RESEARCH_MODE=web`) — гранулярность на уровне ОТДЕЛЬНОГО
  ИСТОЧНИКА/чанка: `extracted_unit_ids` хранит `unit_id` уже обработанных
  единиц (см. `docs_roles_part1.md §3.3`).
- Elaborating (`RESEARCH_MODE=knowledge`) — та же идея, но на уровне
  `subpoint_id` (`extracted_unit_ids` переиспользуется и для этого режима —
  единое поле в `TaskCheckpoint`, несмотря на разные природы "единицы" в
  двух режимах).
- Synthesis/Writer — гранулярность на уровне ОТДЕЛЬНОЙ ЗАМЕТКИ:
  `written_note_indices` хранит индексы уже написанных заметок в `plan.notes`.

### 2.1. `CHECKPOINT_VERSION: int = 4`

**Описание.** Константа-версия схемы чекпоинта. Бампается при
несовместимых изменениях структуры — старые чекпоинты с другой версией
просто НЕ загружаются (`load_checkpoint` вернёт `None`), вместо падения с
невнятной ошибкой валидации Pydantic на устаревших полях. Версия 4
соответствует переименованию `TaskStatus.gemini_calls_used/gemini_calls_log`
→ `llm_calls_used/llm_calls_log` (Gemini исключён как провайдер, остался
только Groq/OpenRouter).

### 2.2. `class TaskCheckpoint(BaseModel)`

**Описание.** Снимок состояния задачи после последнего успешно завершённого шага.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `version` | `int` | Дефолт `CHECKPOINT_VERSION`. |
| `task_id` | `str` | Обязательное. |
| `raw_query` | `str` | Обязательное — исходный запрос (для повторного отображения при resume). |
| `language` | `str` | Обязательное. |
| `last_completed_stage` | `str` | Дефолт `"created"`. Человекочитаемая метка последнего завершённого шага — используется ТОЛЬКО для отображения пользователю (`resumable`/сообщения); логика resume опирается на `*_done` флаги ниже, не на эту строку. |
| `status` | `TaskStatus` | Обязательное. Снимок счётчиков вызовов НА МОМЕНТ последнего `persist(...)` (см. `orchestrator/state_machine.py::run`). |
| `total_llm_calls_used` | `int` | Дефолт `0`. Накопительный расход по ВСЕМ попыткам (для отчёта пользователю). Отдельно от `status.llm_calls_used`, который считает вызовы только в рамках ТЕКУЩЕЙ сессии/попытки — см. `MAX_LLM_CALLS_PER_TASK`: лимит применяется к сессии, иначе задачу нельзя было бы никогда докрутить после однократного исчерпания. |
| `plan` | `Plan \| None` | Дефолт `None`. |
| `plan_approved` | `bool` | Дефолт `False`. Явное подтверждение плана пользователем (`cli/plan_editor.py::confirm_plan`). Отделено от `plan is not None`, т.к. план может быть уже построен (1 дешёвый вызов), но ещё НЕ утверждён — при resume такой задачи подтверждение будет запрошено снова, а не пропущено. |
| `raw_candidates_done` | `bool` | Дефолт `False`. (Web-режим.) |
| `raw_candidates` | `list[SourceCandidate]` | `default_factory=list`. |
| `sources_selected_done` | `bool` | Дефолт `False`. |
| `selected_sources` | `list[SourceCandidate]` | `default_factory=list`. |
| `sources_fetched_done` | `bool` | Дефолт `False`. |
| `fetched_sources` | `list[SourceCandidate]` | `default_factory=list`. |
| `extraction_done` | `bool` | Дефолт `False`. Общий флаг для extracting (web) / elaborating (knowledge). |
| `extracted_unit_ids` | `list[str]` | `default_factory=list`. `unit_id` = `f"{source_id}#{chunk_index}"` (web) либо `subpoint_id` (knowledge) — уже обработанные единицы. |
| `evidence` | `list[Evidence]` | `default_factory=list`. Накапливается по батчам. |
| `vault_analysis_done` | `bool` | Дефолт `False`. Мутирует `plan.notes` напрямую — отдельного списка не нужно. |
| `existing_notes` | `list[ExistingNote]` | `default_factory=list`. |
| `synthesis_done` | `bool` | Дефолт `False`. |
| `written_note_indices` | `list[int]` | `default_factory=list`. Индекс в `plan.notes` — резюм не должен пересчитывать уже написанные заметки (аналогично `extracted_unit_ids` для extraction). |
| `drafts` | `list[DraftNote]` | `default_factory=list`. Накапливается по одной заметке. |
| `relationships` | `list[Relationship]` | `default_factory=list`. |
| `updated_at` | `str` | `default_factory=_now` (локальная копия, не `storage/models.py::_now`, но идентична по смыслу). |

### 2.3. `checkpoint_path(checkpoint_dir: Path, task_id: str) -> Path`

**Описание.** Путь к файлу чекпоинта конкретной задачи.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `checkpoint_dir` | `Path` | Из `settings.checkpoint_dir`. |
| `task_id` | `str` | Идентификатор задачи. |

**Возвращаемое значение:** `Path` — `Path(checkpoint_dir) / f"{task_id}.json"`.

**Исключения:** не поднимает.

### 2.4. `save_checkpoint(checkpoint_dir: Path, checkpoint: TaskCheckpoint) -> None`

**Описание.** Атомарная запись: пишет во временный файл `<task_id>.json.tmp`
и переименовывает поверх основного (`tmp_path.replace(path)`) — так
`kill -9`/сбой посреди записи не оставит битый JSON поверх рабочего чекпоинта.
Актуально, т.к. пишется ЧАСТО (после каждого источника/подпункта на этапе
extracting/elaborating, после каждой написанной заметки на synthesis).
Обновляет `checkpoint.updated_at` прямо перед записью.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `checkpoint_dir` | `Path` | Корневая директория чекпоинтов. |
| `checkpoint` | `TaskCheckpoint` | Что сохранить. |

**Возвращаемое значение:** `None`.

**Исключения:** файловые (`OSError`) — не перехватываются.

**Побочные эффекты:** мутирует `checkpoint.updated_at` (текущее время);
создаёт `checkpoint_dir`, если не существует.

### 2.5. `load_checkpoint(checkpoint_dir: Path, task_id: str) -> TaskCheckpoint | None`

**Описание.** Загружает чекпоинт с диска, с защитой от трёх типов сбоя:
отсутствие файла, повреждённый JSON, несовместимая версия схемы. Во всех
трёх случаях, КРОМЕ отсутствия файла, — логирует ошибку/предупреждение и
возвращает `None` (не поднимает исключение) — вызывающий код
(`Orchestrator._load_or_create_state`) интерпретирует `None` как
"resume невозможен" и поднимает `OrchestratorStopped`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `checkpoint_dir` | `Path` | Корневая директория чекпоинтов. |
| `task_id` | `str` | Идентификатор задачи. |

**Возвращаемое значение:** `TaskCheckpoint | None`:
- `None`, если файл не существует;
- `None` (с `logger.error`), если `json.JSONDecodeError`;
- `None` (с `logger.warning`), если `data.get("version") != CHECKPOINT_VERSION`;
- `None` (с `logger.error`), если `TaskCheckpoint.model_validate(data)` поднял исключение;
- иначе — валидный `TaskCheckpoint`.

**Исключения:** НЕ поднимает — все ожидаемые сбои перехвачены внутри и
превращены в `None` + лог.

### 2.6. `delete_checkpoint(checkpoint_dir: Path, task_id: str) -> None`

**Описание.** Удаляет файл чекпоинта. Вызывается ПОСЛЕ успешного доведения
задачи до `staging` (`Orchestrator.run()`, в самом конце) — чекпоинт больше
не нужен, дальнейшее состояние живёт в `StagingChangeset`, а не в нём.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `checkpoint_dir` | `Path` | Корневая директория чекпоинтов. |
| `task_id` | `str` | Идентификатор задачи. |

**Возвращаемое значение:** `None`.

**Исключения:** не поднимает (`Path.unlink(missing_ok=True)` — тихо
игнорирует отсутствие файла).

### 2.7. `list_resumable_tasks(checkpoint_dir: Path) -> list[TaskCheckpoint]`

**Описание.** Все задачи, у которых есть незавершённый чекпоинт (ещё не
staged) — используется командой `cli/main.py::resumable`. Пропускает файлы
несовместимой версии и повреждённые файлы (с `logger.warning`), не роняя всю
функцию из-за одного плохого файла.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `checkpoint_dir` | `Path` | Корневая директория чекпоинтов. |

**Возвращаемое значение:** `list[TaskCheckpoint]` — отсортировано по имени
файла (`sorted(dir_path.glob("*.json"))`), т.е. примерно по `task_id`, не по
времени. Пустой список, если директория не существует.

**Исключения:** не поднимает — сбои на отдельных файлах перехватываются
внутри цикла (`except Exception as exc: logger.warning(...)`, файл пропускается).

---

## 3. `staging/commit.py` — единственная точка записи в реальный Vault

**Назначение.** Единственная функция во всей системе, которая переносит
staged-изменения в НАСТОЯЩИЙ Vault. Вызывается ТОЛЬКО после явного `approve`
пользователя в CLI (`cli/main.py::approve`). Ничего здесь не делает
"автоматически" в фоне.

### 3.1. `class CommitError(Exception)`

**Описание.** Общий класс ошибок commit-этапа — недостающая валидация,
запрещённые deletes.

### 3.2. `commit_changeset(changeset, vault_path, db, embedder, allow_delete, git_enabled, backup_dir) -> list[WriteResult]`

**Описание.** Применяет `StagingChangeset` к реальному Vault:
1. Защитная проверка: `changeset.validation is None or not changeset.validation.ok`
   → `CommitError` ("это должно быть невозможно при нормальном workflow через
   Orchestrator — остановка как защитная мера").
2. Защитная проверка: `changeset.deletes` непуст, но `allow_delete=False` →
   `CommitError`.
3. Создаёт `VaultWriter(vault_path, allow_delete)` (`vault/writer.py`).
4. Для каждого `draft` в `changeset.creates`: `writer.ensure_folder(draft.folder or "")`,
   затем `writer.write_draft(draft)`.
5. Для каждого `draft` в `changeset.updates`: `writer.write_draft(draft, backup_dir=backup_dir)`
   (с бэкапом старой версии файла).
6. Для каждого пути в `changeset.deletes` (в MVP по умолчанию всегда пуст):
   `writer.delete_note(path)`.
7. Переиндексация: `VaultIndexer(db, vault_path, embedder).sync()` +
   `.resolve_wikilink_targets()` — только затронутых файлов, дёшево, инкрементально
   (реально `sync()` проходит по всему Vault, но пропускает файлы с неизменившимся
   `content_hash` — см. `vault/index.py`).
8. Если `git_enabled=True` — `_git_commit(vault_path, changeset)`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Что применить. |
| `vault_path` | `Path` | Путь к реальному Obsidian Vault. Из `settings.vault_path`. |
| `db` | `VaultDB` | Локальный индекс — для переиндексации после записи. |
| `embedder` | `LocalEmbedder \| None` | Для пересчёта эмбеддингов затронутых заметок при переиндексации. |
| `allow_delete` | `bool` | Из `settings.allow_delete` (дефолт `False`). |
| `git_enabled` | `bool` | Из `settings.git_enabled`. |
| `backup_dir` | `Path` | Куда сохранять snapshot "before" изменяемых (`update`) файлов — обычно `staging_task_dir(...) / "backup_before_update"`. |

**Возвращаемое значение:** `list[WriteResult]` (`vault/writer.py::WriteResult`) —
по одной записи на каждый `create`/`update` (без `delete`), с полями
`path`, `action`, `backup_of` (путь к snapshot, если это был `update`).

**Исключения:**
- `CommitError` — changeset не прошёл валидацию, либо содержит запрещённые deletes.
- `FileExistsError` — из `VaultWriter.write_draft` при `action=CREATE`, если файл
  уже существует ("это должно было быть отловлено Validator-ом раньше").
- `FileNotFoundError` — из `VaultWriter.write_draft` при `action=UPDATE`, если
  целевой файл не существует.
- `PermissionError` — из `VaultWriter.delete_note`, если `allow_delete=False`
  (защита, дублирующая проверку в §2 выше — двойной уровень защиты).

### 3.3. `_git_commit(vault_path: Path, changeset: StagingChangeset) -> None` (приватная)

**Описание.** Опциональный `git add -A` + `git commit` внутри `vault_path`.
Git — вспомогательная функция, НЕ должна ломать основной workflow, если,
например, нечего коммитить или Vault ещё не git-репозиторий — любая
`subprocess.CalledProcessError` перехватывается и только логируется
(`logger.warning`), не пробрасывается наружу.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `vault_path` | `Path` | Путь к Vault (он же git-репозиторий). |
| `changeset` | `StagingChangeset` | Источник текста commit-сообщения (`task_id`, число `creates`/`updates`). |

**Возвращаемое значение:** `None`.

**Исключения:** НЕ поднимает — `subprocess.CalledProcessError` перехвачен внутри.

---

## 4. `staging/diff.py` — человекочитаемый diff для пользователя

**Назначение.** Единственное место, где пользователь ПЕРЕД `approve` видит,
что именно предлагается изменить — включая явные предупреждающие пометки
(`model-knowledge`, `needs_review`), см. `docs/architecture.md §7`.

### 4.1. `_note_markers(d: DraftNote) -> str` (приватная)

**Описание.** Строит короткие текстовые пометки для ОДНОЙ заметки: (1) режим
"без внешних источников" (`frontmatter.source == "model-knowledge"`), (2)
незавершённое критик-ревью (`d.needs_review`). Оба сигнала явно нужны
пользователю ДО `approve`, а не только где-то в логах.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `d` | `DraftNote` | Черновик, для которого строятся пометки. |

**Возвращаемое значение:** `str` — например:
`"⚠ без внешних источников (конспект по знаниям модели)  ⚠ критик не одобрил после 1 попыт(ки/ок) переписывания — проверьте вручную"`,
либо `""`, если пометок нет.

**Исключения:** не поднимает.

### 4.2. `render_diff_summary(changeset: StagingChangeset) -> str`

**Описание.** Строит полный человекочитаемый текстовый diff всего changeset-а
для вывода в консоль (`cli/main.py::_run_and_report`/`approve`). Секции (в
этом порядке, каждая — только если список непуст):
1. `task_id`;
2. `НОВЫЕ ЗАМЕТКИ (N)` — для каждой: путь, заголовок, теги, связи (`[[...]]`),
   `_note_markers`;
3. `ДОПОЛНЯЕМЫЕ ЗАМЕТКИ (N)` — для каждой: путь, первая строка `append_section`
   (обрезана до 80 символов), `_note_markers`;
4. `⚠️ УДАЛЕНИЯ (N) — по умолчанию заблокированы` — список путей;
5. `Новые связи (wikilinks): N` — просто счётчик `changeset.relationships`;
6. `Валидация: OK|ЕСТЬ ОШИБКИ (errors=N, warnings=N)` — только если
   `changeset.validation` заполнен, с построчным списком всех issues
   (маркер `❌` для `error`, `⚠️` для `warning`, формат `[code] message`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Полный changeset для отображения. |

**Возвращаемое значение:** `str` — многострочный текст, готовый для печати
(`console.print(Panel(render_diff_summary(...), ...))` в `cli/main.py`).

**Исключения:** не поднимает.

---

## 5. `staging/draft_merge.py` — объединение уже написанных заметок (без LLM)

**Назначение.** Опциональный финальный шаг ПЕРЕД validation/staging (см.
`config/settings.py::enable_draft_merging`, `orchestrator/state_machine.py`).
Принципиально: здесь НЕТ НИ ОДНОГО вызова LLM. Каждая исходная заметка уже
содержит готовый `body_md` со своими `##`-заголовками (из подпунктов плана);
объединение — это чисто текстовая пересборка: (1) оборачивает `body_md`
каждой исходной заметки в `## {исходный title}`; (2) сдвигает её внутренние
заголовки на 1 уровень глубже (чтобы не столкнуться на одном уровне с
заголовками других объединяемых заметок); (3) объединяет `tags`/`links_out`/
`source_refs` с дедупликацией.

### 5.1. `_shift_headings(text: str, levels: int = 1) -> str` (приватная)

**Описание.** Увеличивает уровень каждого Markdown-заголовка на `levels`
(`## → ###`), НЕ трогая `#`-подобные последовательности внутри fenced code
blocks (` ```...``` `) — иначе строки вида `# comment` в примерах кода
сломались бы. Работает через `re.finditer` по code-fence паттерну, применяя
замену заголовков только к тексту МЕЖДУ блоками кода, оставляя сами блоки
кода нетронутыми.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `text` | `str` | Исходный `body_md` одной заметки. |
| `levels` | `int` (дефолт `1`) | На сколько уровней сдвинуть (`min(len(hashes) + levels, 6)` — не превышает `######`). |

**Возвращаемое значение:** `str` — текст с изменёнными заголовками; пустая строка/пустой ввод возвращается как есть.

**Исключения:** не поднимает.

### 5.2. `merge_drafts(drafts: list[DraftNote], indices: list[int], merged_title: str = "", merged_path: str = "") -> DraftNote`

**Описание.** Объединяет НЕСКОЛЬКО готовых `DraftNote` (только `action=create`)
в ОДИН. Для каждого исходного черновика: `body_parts.append(f"## {d.title}\n\n{_shift_headings(d.body_md.strip())}")`.
Собирает объединённые `tags`/`links_out`/`source_refs` с дедупликацией по
порядку появления (не сортирует). `needs_review`/`critic_rounds` объединённого
черновика — `any(...)`/`max(...)` по исходным (наиболее консервативный вариант:
если хоть одна исходная заметка требует внимания — итоговая тоже помечается).

`note_id` объединённого драфта оставляется ПУСТЫМ (`""`) — он использовался
только для `validate_headings_coverage` (`validation/markdown_validator.py`)
по исходному `Plan`; на этом этапе исходная заметка-план для объединённой
структуры уже не актуальна, проверка просто не выполняется для этого драфта
(`note is None` → no-op в `validate_headings_coverage`) — сознательный компромисс.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Полный список черновиков задачи (не только те, что объединяются). |
| `indices` | `list[int]` | Индексы В `drafts`, которые нужно объединить (минимум 2, дедуплицируются и сортируются внутри функции). |
| `merged_title` | `str` (дефолт `""`) | Заголовок результата; если пусто — `" + ".join(d.title for d in sources)`. |
| `merged_path` | `str` (дефолт `""`) | Путь результата; если пусто — строится через `tools/markdown_tools.py::build_note_path(folder, title)`. |

**Возвращаемое значение:** `DraftNote` — новый объект с `action=NoteAction.CREATE`,
`note_id=""`, `folder=sources[0].folder`, `frontmatter=dict(sources[0].frontmatter)`.

**Исключения:**
- `ValueError` — если после дедупликации `indices` осталось меньше 2 уникальных значений.
- `ValueError` — если какой-то индекс вне диапазона `[0, len(drafts))`.
- `ValueError` — если среди выбранных исходных черновиков есть хоть один с
  `action != CREATE` (объединение `update`-черновиков не поддерживается — у
  них нет самостоятельного `body_md` для рендера как раздела, только
  `append_section` к уже существующему файлу Vault).

### 5.3. `apply_merges(drafts: list[DraftNote], merge_groups: list[tuple[list[int], str]]) -> list[DraftNote]`

**Описание.** Применяет НЕСКОЛЬКО непересекающихся групп слияния за один
проход. Каждая группа — `(индексы, заголовок объединённой заметки)`.
Объединённый драфт встаёт на место ПЕРВОГО (минимального) индекса своей
группы; заметки вне групп сохраняют исходный порядок.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Полный список черновиков. |
| `merge_groups` | `list[tuple[list[int], str]]` | Список `(индексы, заголовок)` — непересекающиеся между собой группы. |

**Возвращаемое значение:** `list[DraftNote]` — тот же порядок, что и `drafts`,
но с объединёнными группами, схлопнутыми в один элемент на позиции минимального
индекса группы; элементы вне групп не изменены.

**Исключения:** `ValueError` — если объединённое множество всех индексов из
ВСЕХ групп содержит дубликаты (группы пересекаются — одна заметка не может
участвовать в двух разных слияниях одновременно).

### 5.4. `merge_all_drafts(drafts: list[DraftNote], merged_title: str = "") -> list[DraftNote]`

**Описание.** Режим `draft_merge_mode="all"` (`config/settings.py`) — объединяет
ВСЕ черновики с `action=CREATE` в ОДИН, без выбора пользователя. Черновики с
`action=UPDATE` (дополнения СУЩЕСТВУЮЩИХ заметок Vault) в объединение НЕ
включаются НИ ПРИ КАКОМ режиме (см. `merge_drafts` про причину). Если
`create`-черновиков меньше двух — сливать нечего, функция возвращает `drafts`
БЕЗ ИЗМЕНЕНИЙ (это НЕ ошибка — например, задача создала одну новую заметку и
дополнила несколько существующих).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Полный список черновиков задачи. |
| `merged_title` | `str` (дефолт `""`) | Заголовок результата (см. `merge_drafts`). |

**Возвращаемое значение:** `list[DraftNote]` — либо `drafts` без изменений
(если `create`-черновиков < 2), либо результат `apply_merges(drafts, [(create_indices, merged_title)])`.

**Исключения:** те же, что у `apply_merges`/`merge_drafts` (хотя при штатном
использовании через `merge_all_drafts` условия ошибок не должны наступать,
т.к. группа строится автоматически и корректно).

---

## Где вызывается объединение в общем workflow

`cli/draft_merge_editor.py::confirm_merges(drafts, mode=settings.draft_merge_mode)`
вызывается из `Orchestrator.run()` ПОСЛЕ `synthesis_done` (все `drafts`
написаны и прошли Critic) и ДО `validation`/`staging` — см.
`orchestrator/state_machine.py`, только если `settings.enable_draft_merging=True`
и передан `merge_confirm_cb`. При `mode="all"` — прямой вызов `merge_all_drafts`.
При `mode="select"` — интерактивный CLI-выбор групп пользователем, затем
`apply_merges`.

---

## Сводная схема потока staging

```
Orchestrator.run() (после synthesis_done)
        │
        ▼
merge_confirm_cb(drafts)  ── опционально, БЕЗ LLM ──► staging/draft_merge.py
        │
        ▼
validation/__init__.py::run_validation(changeset, db, allow_delete, plan)
        │
        ▼
staging/changeset.py::save_changeset(staging_dir, changeset)
        │   ├─ changeset.json (машиночитаемый)
        │   └─ notes_preview/*.md (человекочитаемый)
        │
        ▼  (чекпоинт задачи удаляется — staging/checkpoint.py::delete_checkpoint)
        │
показ diff пользователю (staging/diff.py::render_diff_summary)
        │
        ▼  user: approve
        │
staging/changeset.py::load_changeset(staging_dir, task_id)
        │
        ▼
staging/commit.py::commit_changeset(changeset, vault_path, db, embedder, allow_delete, git_enabled, backup_dir)
        │
        ▼
vault/writer.py::VaultWriter  →  реальные файлы Vault
```

Документация по `staging/` завершена.
