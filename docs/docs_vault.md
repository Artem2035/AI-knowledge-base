# Документация: папка `vault/`

> Слой прямого взаимодействия с Obsidian Vault на файловой системе + локальный
> SQLite-индекс метаданных. Разделение обязанностей строгое: `reader.py`
> ТОЛЬКО читает, `writer.py` ТОЛЬКО пишет и вызывается ИСКЛЮЧИТЕЛЬНО из
> `staging/commit.py` после `approve` (см. `docs_staging.md §3`), `db.py` —
> персистентный кэш метаданных (не дублирует сам Vault), `index.py` —
> инкрементальная синхронизация между файловой системой и `db.py`.

---

## 0. `vault/__init__.py`

Пустой файл-маркер пакета.

---

## 1. `vault/db.py` — SQLite-индекс Vault

**Назначение (из докстринга модуля).** Это НЕ дублирование Vault, а лёгкий
локальный кэш метаданных для быстрого retrieval без парсинга всех файлов на
каждый запрос и без отправки всего Vault в LLM.

**Схема таблиц:**
```sql
notes(path, title, content_hash, raw_content, summary, created_at, updated_at)
frontmatter(note_path, key, value_json)
tags(note_path, tag)
links(source_path, target_path, target_title, link_type)
embeddings(note_path, model, vector_json)   -- опционально
```
`notes.path` — `PRIMARY KEY`. `frontmatter`/`tags` ссылаются на `notes.path`
через `FOREIGN KEY ... ON DELETE CASCADE` — очистка при удалении заметки
из индекса каскадна и не требует ручной чистки в коде. `embeddings` —
составной `PRIMARY KEY (note_path, model)` (одна заметка может иметь
эмбеддинги от разных моделей одновременно, хотя в MVP используется одна).

### 1.1. `class VaultDB`

**Описание.** Обёртка над `sqlite3.Connection` с прикладными методами.
Поддерживает контекстный менеджер (`__enter__`/`__exit__` → `close()`).

#### `__init__(self, db_path: Path)`

**Описание.** Создаёт родительскую директорию файла БД (если нужно),
открывает соединение (`row_factory = sqlite3.Row` — доступ к колонкам по
имени), включает `PRAGMA foreign_keys = ON` (обязательно для `ON DELETE
CASCADE`), выполняет `SCHEMA` (идемпотентно, `CREATE TABLE IF NOT EXISTS`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `db_path` | `Path` | Путь к файлу SQLite. Из `settings.db_path`. |

**Возвращаемое значение:** — (конструктор).
**Исключения:** `sqlite3.Error` при проблемах с БД — не перехватывается.

#### `close(self) -> None`

Закрывает соединение (`self.conn.close()`).

#### `upsert_note(self, path, title, content_hash, raw_content, summary, frontmatter, tags, created_at, updated_at) -> None`

**Описание.** Вставляет ИЛИ обновляет запись заметки (`INSERT ... ON CONFLICT(path)
DO UPDATE SET ...` — обновляются все поля, КРОМЕ `created_at`, который
сохраняется от первой вставки). Затем полностью пересоздаёт связанные
`frontmatter`/`tags` для этого пути (`DELETE` + `INSERT` заново) — не
инкрементальный diff полей, а полная замена. Коммитит транзакцию в конце.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `path` | `str` | Относительный posix-путь заметки внутри Vault. |
| `title` | `str` | Заголовок. |
| `content_hash` | `str` | Хэш сырого содержимого (см. `vault/reader.py::_compute_hash`) — ключ инкрементальности. |
| `raw_content` | `str` | Полный сырой текст файла (с frontmatter). |
| `summary` | `str` | Краткое описание (`vault/reader.py::_make_summary`). |
| `frontmatter` | `dict[str, Any]` | YAML-метаданные заметки. |
| `tags` | `Iterable[str]` | Список тегов. |
| `created_at` | `str` | ISO-время (используется только при первой вставке). |
| `updated_at` | `str` | ISO-время. |

**Возвращаемое значение:** `None`.
**Исключения:** `sqlite3.Error` — не перехватывается.

#### `note_exists_with_hash(self, path: str, content_hash: str) -> bool`

**Описание.** Проверяет, есть ли в индексе заметка с ЭТИМ путём И ЭТИМ хэшем
одновременно — если да, файл не изменился с прошлой индексации, парсинг/
переиндексация не нужны (ключевая точка инкрементальности `vault/index.py`).

**Параметры:** `path: str`, `content_hash: str`.
**Возвращаемое значение:** `bool`.
**Исключения:** не поднимает (кроме `sqlite3.Error`).

#### `get_all_notes(self) -> list[sqlite3.Row]`

Все строки таблицы `notes`. Используется `retrieval/search.py::VaultSearcher`
для построения BM25-индекса и `vault_analyst.py` (через `db.get_distinct_folders`).

#### `get_note(self, path: str) -> sqlite3.Row | None`

Одна строка `notes` по пути, либо `None`.

#### `delete_note(self, path: str) -> None`

**Описание.** Удаляет запись из ВСЕХ таблиц по этому пути (`notes`,
`frontmatter`, `tags`, `links` — где `source_path = path`, `embeddings`).
Коммитит. Вызывается `VaultIndexer.sync()` для заметок, которые исчезли из
файловой системы Vault (не то же самое, что `ALLOW_DELETE` — это про синк
индекса с уже случившимся изменением на диске, а не про удаление файла).

**Параметры:** `path: str`.
**Возвращаемое значение:** `None`.
**Исключения:** `sqlite3.Error` — не перехватывается.

#### `get_all_paths(self) -> set[str]`

Множество всех путей в индексе. Используется `VaultIndexer.sync()` (сравнение
с текущим набором файлов на диске) и `validation/link_validator.py::validate_no_path_collisions`.

#### `get_distinct_folders(self) -> list[str]`

**Описание.** Список УНИКАЛЬНЫХ папок (без имени файла), уже встречающихся в
индексе Vault — используется `vault_analyst.py::_assign_folders_batch`,
чтобы предлагать переиспользование существующей структуры папок вместо того,
чтобы все новые заметки по умолчанию складывались в одну плоскую
`default_notes_folder`.

**Возвращаемое значение:** `list[str]` — отсортирован (`sorted(folders)`).
Папка определяется как `path.rsplit("/", 1)[0]`, если в пути есть `/`;
заметки в корне Vault (без папки) не дают записи в этот список.

#### `set_links(self, source_path: str, links: list[tuple[str | None, str, str]]) -> None`

**Описание.** Полностью пересоздаёт исходящие ссылки для `source_path`
(`DELETE WHERE source_path = ?` + `INSERT` заново для каждой связи).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `source_path` | `str` | Путь заметки-источника. |
| `links` | `list[tuple[str \| None, str, str]]` | Список `(target_path_or_None, target_title, link_type)`. `target_path` может быть `None`, если целевая заметка ещё не проиндексирована/не существует (нерезолвленный wikilink). |

**Возвращаемое значение:** `None`.

#### `get_backlinks(self, target_path: str) -> list[str]`

Список `source_path` всех заметок, ссылающихся на `target_path` (по
`target_path`, а НЕ по `target_title` — т.е. только УЖЕ РЕЗОЛВЛЕННЫЕ ссылки).

#### `get_outlinks(self, source_path: str) -> list[str]`

Список `target_title` всех исходящих ссылок заметки `source_path` (независимо
от того, резолвлены ли они в путь).

#### `get_all_tags(self) -> list[str]`

Список УНИКАЛЬНЫХ тегов по всему индексу, отсортирован.

#### `set_embedding(self, note_path: str, model: str, vector: list[float]) -> None`

Вставляет/обновляет вектор эмбеддинга (`INSERT ... ON CONFLICT(note_path, model)
DO UPDATE`). Вектор хранится как JSON-строка (`json.dumps(vector)`).

#### `get_embedding(self, note_path: str, model: str) -> list[float] | None`

Вектор для конкретной пары (заметка, модель), либо `None`, если не найден.

#### `get_all_embeddings(self, model: str) -> dict[str, list[float]]`

Все векторы конкретной модели, ключ — `note_path`.

**Исключения для всех методов `VaultDB` (кроме перечисленных явно):**
`sqlite3.Error` и подклассы — намеренно не перехватываются нигде в классе,
падение БД — нештатная ситуация, которую лучше не глотать молча.

---

## 2. `vault/reader.py` — прямой доступ к Vault через файловую систему

**Назначение (из докстринга модуля).** Реализует "Вариант A" (прямой доступ
к файловой системе, без Obsidian API/плагина). Никогда не пишет в Vault —
только читает. Запись — исключительно через `vault/writer.py`, и только на
этапе Commit (после `approve`).

### 2.1. Регулярные выражения модуля

| Имя | Паттерн | Назначение |
|---|---|---|
| `WIKILINK_RE` | `\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]` | Извлекает заголовок цели из `[[Title]]`, `[[Title#Heading]]`, `[[Title|Alias]]` — во всех случаях группа 1 — чистый `Title`. |
| `INLINE_TAG_RE` | `(?<!\S)#([\w\-/А-Яа-яЁё]+)` | Инлайн-теги вида `#tag` в теле заметки (не после несимвольного пробельного разделителя слева — `(?<!\S)` — чтобы не срабатывать на `#` внутри слова/URL). Поддерживает кириллицу и `/` (вложенные теги Obsidian). |

### 2.2. `class ParsedNote` (dataclass)

**Описание.** Результат парсинга одного файла Markdown — НЕ Pydantic-модель
(обычный `@dataclass`), т.к. не пересекает LLM-контракт напрямую.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Относительный путь внутри Vault, POSIX-стиль (`/`, не `\`). |
| `title` | `str` | Из `frontmatter.title`, либо имя файла без расширения (fallback). |
| `frontmatter` | `dict` | `default_factory=dict`. Полный YAML frontmatter as-is. |
| `tags` | `list[str]` | `default_factory=list`. Объединение frontmatter-тегов и инлайн-тегов, отсортировано. |
| `body` | `str` | `default_factory=str`. Тело заметки БЕЗ frontmatter-блока. |
| `raw_content` | `str` | `default_factory=str`. Полный файл as-is (frontmatter + тело). |
| `outlinks` | `list[str]` | `default_factory=list`. Заголовки, на которые ссылается заметка (`[[...]]`), отсортированы, без дублей. |
| `content_hash` | `str` | `default_factory=str`. См. `_compute_hash`. |

### 2.3. `_compute_hash(raw_content: str) -> str` (приватная)

**Описание.** SHA-256 от UTF-8 байт содержимого, обрезанный до первых 16
hex-символов (64 бита) — компактный, но практически коллизионно-безопасный
для задачи "изменился ли файл".

**Параметры:** `raw_content: str`.
**Возвращаемое значение:** `str` — 16 hex-символов.
**Исключения:** не поднимает.

### 2.4. `_extract_tags(frontmatter_data: dict, body: str) -> list[str]` (приватная)

**Описание.** Собирает теги из ДВУХ источников: (1) `frontmatter_data.get("tags")` —
поддерживает и строку (одиночный тег, `.lstrip("#")`), и список (каждый
элемент через `str(t).lstrip("#")`); (2) все инлайн-совпадения `INLINE_TAG_RE`
в `body`. Объединяет через `set`, убирая дубли.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `frontmatter_data` | `dict` | YAML-метаданные заметки (`post.metadata` из `python-frontmatter`). |
| `body` | `str` | Тело заметки (без frontmatter). |

**Возвращаемое значение:** `list[str]` — отсортированный список уникальных тегов.
**Исключения:** не поднимает.

### 2.5. `_extract_wikilinks(body: str) -> list[str]` (приватная)

**Описание.** Все уникальные заголовки-цели `[[wikilink]]` в теле, отсортированные.

**Параметры:** `body: str`.
**Возвращаемое значение:** `list[str]`.
**Исключения:** не поднимает.

### 2.6. `_make_summary(body: str, max_chars: int = 400) -> str`

**Описание.** Простая ДЕТЕРМИНИРОВАННАЯ summary БЕЗ LLM: берёт непустые строки
тела, не начинающиеся с `#` (т.е. не заголовки), склеивает через пробел,
обрезает до `max_chars` по границе слова (`.rsplit(" ", 1)`) с добавлением
`…`, если текст длиннее.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `body` | `str` | Тело заметки. |
| `max_chars` | `int` (дефолт `400`) | Лимит длины summary. |

**Возвращаемое значение:** `str`. Если текст короче `max_chars` — возвращается целиком, без обрезки/многоточия.
**Исключения:** не поднимает.

**Публичность:** экспортируется из `__all__` и переиспользуется в
`vault/index.py::VaultIndexer._upsert` (summary пересчитывается при каждой
переиндексации изменившейся заметки).

### 2.7. `parse_note_file(vault_path: Path, file_path: Path) -> ParsedNote`

**Описание.** Читает один файл, парсит через `python-frontmatter`
(`fm.loads(raw)` — разделяет YAML-заголовок и тело), строит относительный
POSIX-путь (`file_path.relative_to(vault_path).as_posix()`), извлекает
теги/wikilinks, вычисляет хэш.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `vault_path` | `Path` | Корень Vault — для вычисления относительного пути. |
| `file_path` | `Path` | Абсолютный путь конкретного файла. |

**Возвращаемое значение:** `ParsedNote`.

**Исключения:** `OSError`/`UnicodeDecodeError` при чтении файла (кодировка не
UTF-8 или файл недоступен) — не перехватываются; ошибки парсинга YAML от
`python-frontmatter` также не перехватываются явно здесь.

### 2.8. `iter_markdown_files(vault_path: Path)` (генератор)

**Описание.** Итерирует все `.md`-файлы Vault в отсортированном порядке
(`sorted(vault_path.rglob("*.md"))`), пропуская файлы внутри СКРЫТЫХ папок
(любая часть пути, начинающаяся с `.`, КРОМЕ имени самого файла) — например,
`.obsidian/` (служебная папка Obsidian) и любые рабочие директории проекта,
если они случайно оказались внутри Vault.

**Параметры:** `vault_path: Path`.
**Возвращаемое значение:** генератор `Path`.
**Исключения:** не поднимает явно (может поднять `OSError` при проблемах ФС во время обхода).

### 2.9. `read_vault(vault_path: Path) -> list[ParsedNote]`

**Описание.** Главная публичная функция чтения — парсит ВСЕ Markdown-файлы Vault.

**Параметры:** `vault_path: Path`.
**Возвращаемое значение:** `list[ParsedNote]` — по одному на каждый файл,
в порядке `iter_markdown_files`.
**Исключения:** `FileNotFoundError` — если `vault_path` не существует
(явная проверка `if not vault_path.exists()` в начале функции, до обхода).

### 2.10. `read_single_note(vault_path: Path, rel_path: str) -> ParsedNote | None`

**Описание.** Читает ОДНУ конкретную заметку по относительному пути.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `vault_path` | `Path` | Корень Vault. |
| `rel_path` | `str` | Относительный путь заметки. |

**Возвращаемое значение:** `ParsedNote | None` — `None`, если файл не существует.
**Исключения:** те же, что у `parse_note_file`, если файл существует, но не читается.

### 2.11. `__all__`

Явный экспорт: `ParsedNote`, `parse_note_file`, `iter_markdown_files`,
`read_vault`, `read_single_note`, `_make_summary` (единственная "приватная по
имени" функция, явно экспортируемая — переиспользуется в `vault/index.py`).

---

## 3. `vault/index.py` — построение и инкрементальное обновление индекса

**Назначение (из докстринга модуля).** Инкрементальность — ключевое свойство:
если для файла уже есть запись в `notes` с ТЕМ ЖЕ `content_hash` — файл НЕ
перепарсивается, эмбеддинг НЕ пересчитывается. Это то, что позволяет НЕ
пересканировать весь Vault на каждый запрос.

### 3.1. `_now() -> str` (приватная, модульная)

Локальная копия того же паттерна, что и `storage/models.py::_now`
(`datetime.now(timezone.utc).isoformat()`) — не импортируется оттуда,
самостоятельное определение в этом модуле.

### 3.2. `class VaultIndexer`

#### `__init__(self, db: VaultDB, vault_path: Path, embedder=None)`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `db` | `VaultDB` | Куда писать индекс. |
| `vault_path` | `Path` | Корень Vault для чтения. |
| `embedder` | `tools.dedup.LocalEmbedder \| None` (дефолт `None`) | Если задан — при переиндексации изменившихся заметок дополнительно пересчитывается эмбеддинг. |

**Возвращаемое значение:** — (конструктор, только сохраняет атрибуты).

#### `sync(self) -> dict`

**Описание.** Полный проход по Vault с ИНКРЕМЕНТАЛЬНЫМ апдейтом индекса:
1. `notes = read_vault(self.vault_path)` — парсит все файлы на диске.
2. `current_paths = {n.path for n in notes}`, `existing_paths = self.db.get_all_paths()`.
3. Для каждой распарсенной заметки: если `db.note_exists_with_hash(note.path,
   note.content_hash)` — `unchanged += 1`, `continue` (пропуск дорогого
   апдейта); иначе — `self._upsert(note)`, `updated += 1`.
4. `removed_paths = existing_paths - current_paths` — заметки, которые есть в
   индексе, но исчезли из файловой системы (пользователь мог удалить/переместить
   их вручную в Obsidian, вне системы) — для каждой вызывается `db.delete_note(path)`.
   Это НЕ то же самое, что удаление файла системой — файла уже нет, поэтому
   `ALLOW_DELETE` здесь не применим (см. докстринг метода в коде).

**Параметры:** нет (использует атрибуты `self`).

**Возвращаемое значение:** `dict` — `{"scanned": N, "updated": N, "unchanged": N,
"removed": N}`, где `scanned = len(notes)` (сколько файлов реально прочитано с
диска на этот вызов, включая неизменившиеся).

**Исключения:** пробрасывает `FileNotFoundError` от `read_vault`, если
`vault_path` не существует; `sqlite3.Error` от операций `db`.

#### `_upsert(self, note: ParsedNote) -> None` (приватный)

**Описание.** Пересчитывает `summary` (`_make_summary(note.body)`), вызывает
`db.upsert_note(...)` со ВСЕМИ полями (в т.ч. одинаковыми `created_at`/`updated_at`
на КАЖДЫЙ апдейт — реальный `created_at` при этом в БД НЕ меняется, т.к.
`upsert_note` в `ON CONFLICT` не трогает это поле, см. §1). Строит и сохраняет
исходящие ссылки: `links = [(None, title, "wikilink") for title in
note.outlinks]` — на этом шаге `target_path` ВСЕГДА `None` (первый проход,
резолвинг происходит позже, во втором проходе `resolve_wikilink_targets`).
Если задан `embedder` — пытается пересчитать и сохранить эмбеддинг
(`embedder.embed(f"{note.title}\n{summary}")`), с защитой от сбоя: любое
исключение перехватывается и логируется как `logger.warning(...)` — эмбеддинги
best-effort, не должны ломать индексацию.

**Параметры:** `note: ParsedNote`.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает наружу (ошибки эмбеддера перехвачены внутри;
ошибки `db.upsert_note`/`db.set_links` — теоретически могут поднять `sqlite3.Error`,
не перехвачены явно).

#### `resolve_wikilink_targets(self) -> None`

**Описание.** ВТОРОЙ проход индексации: сопоставляет `target_title → target_path`
там, где заметка с таким заголовком СУЩЕСТВУЕТ в индексе (нужно для
backlinks — `db.get_backlinks` работает по `target_path`, а не по
`target_title`). Строит карту `title_to_path` по ВСЕМ заметкам индекса,
затем для КАЖДОЙ заметки перечитывает её `outlinks` (`db.get_outlinks`) и
пересобирает список связей с резолвленными путями (`title_to_path.get(t)`,
`None`, если заголовок не найден — "красная" ссылка), сохраняет через
`db.set_links` заново (перезаписывая связи из первого прохода `_upsert`).

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает явно (кроме возможных `sqlite3.Error`).

**Важно:** вызывается ОТДЕЛЬНО от `sync()` (не внутри неё) — оба метода
вызываются последовательно везде, где используются (`cli/main.py::index`,
`Orchestrator.sync_vault_index`, `staging/commit.py::commit_changeset`),
т.к. `resolve_wikilink_targets` логически зависит от того, что ВСЕ заметки
(включая только что добавленные) уже в индексе.

---

## 4. `vault/writer.py` — единственная точка записи в реальный Vault

**Назначение (из докстринга модуля, КРИТИЧЕСКОЕ ПРАВИЛО ПРОЕКТА).** Этот
модуль вызывается ТОЛЬКО из staging/commit-логики (`staging/commit.py`), и
только ПОСЛЕ явного `approve` пользователя в CLI. Ничего в `roles/*`,
`llm/*` или `retrieval/*` не должно импортировать этот модуль напрямую.

### 4.1. `class WriteResult` (dataclass)

**Описание.** Результат одной операции записи — возвращается
`VaultWriter.write_draft`, собирается в список `staging/commit.py::commit_changeset`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `path` | `str` | Путь записанного файла. |
| `action` | `str` | `"create"` или `"update"` (значение `NoteAction`, не сам Enum). |
| `backup_of` | `str \| None` (дефолт `None`) | Путь к snapshot "before", если это был `UPDATE` и `backup_dir` был передан. |

### 4.2. `class VaultWriter`

#### `__init__(self, vault_path: Path, allow_delete: bool = False)`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `vault_path` | `Path` | Корень реального Vault. |
| `allow_delete` | `bool` (дефолт `False`) | Разрешено ли удаление файлов через `delete_note`. |

**Возвращаемое значение:** — (конструктор).

#### `write_draft(self, draft: DraftNote, backup_dir: Path | None = None) -> WriteResult`

**Описание.** Пишет ОДИН черновик в реальный файл:

**Для `action == NoteAction.CREATE`:**
- Если `full_path` уже существует — `FileExistsError` ("это должно было быть
  отловлено Validator-ом раньше" — защитная мера, а не ожидаемый путь).
- Создаёт родительские папки (`full_path.parent.mkdir(parents=True, exist_ok=True)`).
- Пишет `render_markdown(draft)` (`tools/markdown_tools.py`) полностью.

**Для `action == NoteAction.UPDATE`:**
- Если `full_path` НЕ существует — `FileNotFoundError`.
- Если `backup_dir` передан — создаёт его, сохраняет ТЕКУЩЕЕ содержимое файла
  в `backup_dir / draft.path.replace("/", "__")` (snapshot "before"),
  запоминает путь в `backup_of`.
- Если `draft.append_section` непусто — читает существующий файл, дописывает
  `"\n\n" + sanitize_wikilinks(draft.append_section.strip()) + "\n"` в конец
  (`existing.rstrip()` перед этим — не накапливает лишние пустые строки),
  перезаписывает файл целиком новым содержимым (существующее содержимое НЕ
  теряется — оно часть нового текста).
- Если `append_section` пусто — ПОЛНОСТЬЮ перезаписывает файл через
  `render_markdown(draft)` (используется, когда update не через append, а
  через замену всего тела).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `draft` | `DraftNote` | Черновик для записи. |
| `backup_dir` | `Path \| None` (дефолт `None`) | Куда сохранять snapshot "before" при update. Если `None` — бэкап не делается (используется в тестах/при `action=CREATE`, где бэкап не нужен). |

**Возвращаемое значение:** `WriteResult`.

**Исключения:**
- `FileExistsError` — `CREATE` на уже существующий путь.
- `FileNotFoundError` — `UPDATE` на несуществующий путь.
- `OSError` — прочие файловые сбои (не перехватываются).

#### `delete_note(self, rel_path: str) -> None`

**Описание.** Удаляет файл по относительному пути, ЕСЛИ `self.allow_delete=True`.
Иначе — `PermissionError` (защита по умолчанию, НАМЕРЕННО не обходится
автоматически, см. докстринг метода в коде). Если файл не существует —
тихо ничего не делает (`if full_path.exists(): full_path.unlink()`).

**Параметры:** `rel_path: str`.
**Возвращаемое значение:** `None`.
**Исключения:** `PermissionError` — если `allow_delete=False`.

#### `ensure_folder(self, rel_folder: str) -> None`

**Описание.** Создаёт папку внутри Vault, если не существует
(`(self.vault_path / rel_folder).mkdir(parents=True, exist_ok=True)`).
Вызывается `staging/commit.py::commit_changeset` перед записью КАЖДОГО
`create`-черновика (даже если папка уже точно существует — операция
идемпотентна и дешёвая).

**Параметры:** `rel_folder: str`.
**Возвращаемое значение:** `None`.
**Исключения:** `OSError` при проблемах ФС (права доступа и т.п.).

---

## Сводная схема: поток данных через `vault/`

```
Чтение (не пишет никогда):
    vault/reader.py::read_vault(vault_path) ──► list[ParsedNote]
        │
        ▼
    vault/index.py::VaultIndexer.sync() ──► инкрементальный upsert в vault/db.py::VaultDB
        │
        ▼
    VaultIndexer.resolve_wikilink_targets() ──► второй проход: target_title → target_path

Retrieval (без LLM, без записи):
    retrieval/search.py::VaultSearcher(db, embedder) ──► читает VaultDB.get_all_notes()/get_embedding()

Запись (ТОЛЬКО после approve):
    staging/commit.py::commit_changeset(...)
        │
        ▼
    vault/writer.py::VaultWriter.write_draft(draft, backup_dir) ──► реальный файл на диске
        │
        ▼
    VaultIndexer(db, vault_path, embedder).sync() + .resolve_wikilink_targets()  — переиндексация после записи
```

Документация по `vault/` завершена.
