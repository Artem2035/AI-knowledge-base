# Документация: папка `tools/`

> "Skills" в терминологии `docs/architecture.md` — переиспользуемые
> детерминированные функции без LLM, которыми пользуются роли (`roles/*.py`).
> Ничего в этой папке не вызывает `client.generate_structured(...)` — это
> отличает `tools/` от `roles/`: роль = LLM-вызов + сборка промпта, skill =
> чистый код общего назначения.

---

## 0. `tools/__init__.py`

Пустой файл-маркер пакета.

---

## 1. `tools/web_search.py` — skill "web search" (только `RESEARCH_MODE=web`)

**Назначение (из докстринга модуля).** Только бесплатный DuckDuckGo через
`ddgs`, без ключей, без платных провайдеров, никакого fallback на
Tavily/SerpAPI/etc — прямое следствие ограничения `FREE_ONLY`.

### 1.1. `search_web(query: str, max_results: int = 6, subtopic: str = "") -> list[SourceCandidate]`

**Описание.** Возвращает СЫРЫЕ кандидаты источников — ранжирование/отбор
релевантности выполняется ОТДЕЛЬНЫМ шагом (`roles/researcher.py::select_relevant_sources`,
LLM-вызов), здесь только сбор сырых результатов поиска. Локальный импорт
`from ddgs import DDGS` — модуль не должен требовать пакет `ddgs`, если он
не используется (это же правило применяется во всём проекте к необязательным
зависимостям, см. `tools/dedup.py::LocalEmbedder._ensure_loaded`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `query` | `str` | Поисковый запрос. |
| `max_results` | `int` (дефолт `6`) | Сколько результатов запросить у DuckDuckGo. Из `settings.max_search_results_per_query`. |
| `subtopic` | `str` (дефолт `""`) | К какой подтеме плана относится запрос — проставляется каждому найденному `SourceCandidate.subtopic`. |

**Возвращаемое значение:** `list[SourceCandidate]` — по одному на каждый
результат `DDGS().text(query, max_results=max_results)`, с полями `url`
(`r.get("href", "")`), `title`, `snippet` (`r.get("body", "")`), `subtopic`.

**Исключения:**
- `RuntimeError` — если пакет `ddgs` не установлен (`ImportError` перехвачен
  и переброшен как понятный `RuntimeError` с подсказкой `pip install ddgs`).
- Сетевые/поисковые сбои (любой `Exception` внутри `with DDGS() as ddgs:`) —
  НЕ пробрасываются наружу: перехватываются, логируются
  (`logger.warning("Веб-поиск не удался для запроса '%s': %s", query, exc)`),
  функция возвращает то, что успела собрать до сбоя (может быть пустым списком).

### 1.2. `deduplicate_by_url(candidates: list[SourceCandidate]) -> list[SourceCandidate]`

**Описание.** Убирает дубликаты по URL, нормализуя перед сравнением
(`c.url.rstrip("/").lower()`) — сохраняет ПЕРВОЕ вхождение каждого
уникального (нормализованного) URL, порядок остальных элементов сохраняется.
Пустые URL (`norm` falsy) пропускаются без добавления в результат.

**Параметры:** `candidates: list[SourceCandidate]`.
**Возвращаемое значение:** `list[SourceCandidate]` — без дублей по URL.
**Исключения:** не поднимает.

---

## 2. `tools/web_fetch.py` — skill "URL fetching + page extraction" (только `RESEARCH_MODE=web`)

**Назначение (из докстринга модуля).** Бесплатно: `httpx` (HTTP-клиент) +
`trafilatura` (извлечение чистого текста из HTML, без рекламы/навигации/шума).

### 2.1. Константы модуля

| Имя | Значение | Назначение |
|---|---|---|
| `USER_AGENT` | `"Mozilla/5.0 (compatible; ObsidianAIKB/0.1; personal-use-research-bot)"` | Идентифицирует бота как персональный research-инструмент, а не маскируется под браузер — этичная практика для скрейпинга. |
| `MAX_CHARS_PER_SOURCE` | `12000` | Ограничивает объём текста ОДНОГО источника — чтобы не раздувать контекст, передаваемый дальше в LLM (`roles/extractor_critic.py`). |

### 2.2. `fetch_clean_text(url: str, timeout: float = 15.0) -> tuple[str | None, str | None]`

**Описание.** Скачивает страницу (`httpx.Client(follow_redirects=True,
timeout=timeout, headers={"User-Agent": USER_AGENT})`, `resp.raise_for_status()`),
затем извлекает основной текст через `trafilatura.extract(html,
include_comments=False, include_tables=False)`. Обрезает результат до
`MAX_CHARS_PER_SOURCE` символов, если он длиннее.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `url` | `str` | Адрес страницы. |
| `timeout` | `float` (дефолт `15.0`) | Таймаут HTTP-запроса в секундах. |

**Возвращаемое значение:** `tuple[str | None, str | None]` — `(text, error)`.
Ровно один из двух элементов непуст:
- Успех: `(extracted_text, None)`.
- Сбой HTTP (сетевая ошибка, не-2xx статус): `(None, "fetch_error: {exc}")`.
- Сбой извлечения (`trafilatura` бросил исключение): `(None, "extract_error: {exc}")`.
- Извлечение вернуло пусто (`not extracted`): `(None, "extract_error: empty content")`.

**Исключения:** НЕ поднимает — все возможные сбои (HTTP, парсинг) перехвачены
внутри функции и превращены во второй элемент кортежа.

---

## 3. `tools/markdown_tools.py` — skill "Markdown generation" (принадлежит роли Writer)

**Назначение (из докстринга модуля).** Реализован как чистая,
детерминированная функция без LLM — генерация текста YAML/Markdown не требует
reasoning, только форматирование уже готовых структурированных данных из
`DraftNote`.

### 3.1. Регулярные выражения и константы модуля

| Имя | Значение/паттерн | Назначение |
|---|---|---|
| `_INVALID_FS_CHARS` | `[\\/:*?"<>\|#^\[\]]` | Символы, недопустимые в именах файлов на большинстве ФС (в т.ч. Windows) — вырезаются из заголовка при построении имени файла. |
| `_NESTED_WIKILINK_RE` | `\[{2,}([^\[\]]+)\]{2,}` | Находит вложенные/задублированные квадратные скобки вокруг wikilink-заголовка (баг генерации LLM, см. `sanitize_wikilinks`). |
| `_DASH_VARIANTS` | `dict[str, str]` | Unicode-варианты тире/дефиса (`‐‑‒–—―` → обычный `-`) — для сравнения заголовков без учёта визуально неразличимых вариантов дефиса. |
| `_ALLOWED_FRONTMATTER_KEYS` | `("title", "tags", "created", "source")` | **Единственный источник истины** по составу YAML frontmatter — сознательно ограничен по требованию продукта (не плодить произвольные YAML-свойства, которые может насочинять LLM через `frontmatter_extra`). Контроль на уровне КОДА, а не промпта: даже если промпт Writer-роли изменится и снова начнёт предлагать другие ключи, лишнее сюда не попадёт. `"source"` добавлен для `RESEARCH_MODE=knowledge` — `roles/synthesizer_writer.py::_to_draft_note` проставляет `frontmatter["source"] = "model-knowledge"`. |

### 3.2. `slugify_filename(title: str) -> str`

**Описание.** Делает безопасное имя файла для Obsidian, СОХРАНЯЯ кириллицу
(в отличие от типичных web-slugify — по ТЗ проекта имена заметок по умолчанию
на русском, если запрос на русском). NFC-нормализация Unicode, замена `/`/`\`
на пробел, вырезание `_INVALID_FS_CHARS`, схлопывание множественных пробелов.

**Параметры:** `title: str`.
**Возвращаемое значение:** `str` — безопасное имя, либо `"Без названия"`, если после очистки получилась пустая строка.
**Исключения:** не поднимает.

### 3.3. `build_note_path(folder: str, title: str) -> str`

**Описание.** Строит относительный путь заметки: `{folder}/{slugify(title)}.md`,
либо `{slugify(title)}.md`, если папка пуста. Обрезает лишние `/` по краям
`folder` (`folder.strip("/")`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `folder` | `str` | Папка (может быть пустой или с вложенностью `"Знания/RAG"`). |
| `title` | `str` | Заголовок заметки. |

**Возвращаемое значение:** `str` — POSIX-путь с расширением `.md`.
**Исключения:** не поднимает.

### 3.4. `render_frontmatter(frontmatter: dict) -> str`

**Описание.** Строит YAML frontmatter-блок, ограниченный `_ALLOWED_FRONTMATTER_KEYS`
(в этом порядке — `title`/`tags`/`created`/`source`, `sort_keys=False`
сохраняет именно этот порядок, а не алфавитный). Пропускает ключи, которых
нет во входном `frontmatter` ИЛИ значение которых falsy (пустая строка/список).
Если `created` не задан явно — подставляет сегодняшнюю дату UTC как дефолт
(`ordered.setdefault(...)`) — гарантирует, что `created` ВСЕГДА присутствует
в итоговом YAML.

**Параметры:** `frontmatter: dict` — сырой словарь (может содержать посторонние
ключи — они будут отфильтрованы).

**Возвращаемое значение:** `str` — например:
```
---
title: Reranking
tags:
- rag
created: '2026-09-26'
---
```
(`yaml.safe_dump(..., allow_unicode=True, sort_keys=False, default_flow_style=False)`,
обёрнутый в `---\n...---\n`).

**Исключения:** не поднимает (кроме теоретических ошибок `yaml.safe_dump` на
несериализуемых значениях — на практике `frontmatter` содержит только
примитивы/списки примитивов).

### 3.5. `render_sources_block(source_refs: list[str]) -> str`

**Описание.** Источники — простой список ссылок В НАЧАЛЕ тела заметки (сразу
после frontmatter), а НЕ свойство YAML (см. `render_frontmatter` — намеренно
не пропускает произвольные ключи, кроме разрешённых). В `RESEARCH_MODE=knowledge`
`source_refs` ВСЕГДА пуст (нет внешних источников) — тогда блок просто не
рендерится.

**Параметры:** `source_refs: list[str]` — список URL.

**Возвращаемое значение:** `str` — блок вида `"## Источники\n\n- url1\n- url2\n\n"`,
либо `""`, если `source_refs` пуст.

**Исключения:** не поднимает.

### 3.6. `render_markdown(draft: DraftNote) -> str`

**Описание.** Главная публичная функция — собирает ПОЛНЫЙ Markdown-файл
заметки из `DraftNote`. Порядок сборки:
1. `fm = dict(draft.frontmatter)`, добавляет `fm["title"] = draft.title` и,
   если непусто, `fm["tags"] = draft.tags` (эти два поля ВСЕГДА берутся из
   `draft`, а не из уже присутствующих ключей `draft.frontmatter`, если они
   там случайно были);
2. `render_frontmatter(fm)`;
3. `render_sources_block(draft.source_refs)`;
4. тело: `sanitize_wikilinks(draft.body_md.strip())`;
5. если `draft.links_out` непуст — добавляет секцию
   `"\n## Связанные заметки\n\n{список [[title]]}\n"`, где КАЖДЫЙ элемент
   прогоняется через `strip_wikilink_brackets` перед обёртыванием в `[[...]]`
   заново (защита от двойного оборачивания, см. §3.9).

**Параметры:** `draft: DraftNote`.

**Возвращаемое значение:** `str` — полный текст файла, готовый к записи
(`vault/writer.py::VaultWriter.write_draft` использует эту функцию для
`action=CREATE` и для `action=UPDATE` без `append_section`).

**Исключения:** не поднимает.

### 3.7. `insert_wikilinks(body_md: str, titles_to_link: list[str]) -> str`

**Описание.** Простая ДЕТЕРМИНИРОВАННАЯ простановка `[[wikilink]]` для
ПЕРВОГО вхождения КАЖДОГО заголовка из `titles_to_link` в тексте (regex с
границами слова `\b`, не задевает уже существующие ссылки благодаря
негативному lookbehind `(?<!\[)` и негативному lookahead `(?!\])`). Обрабатывает
заголовки в порядке УБЫВАНИЯ ДЛИНЫ (`sorted(..., key=len, reverse=True)`) —
чтобы более длинный заголовок, содержащий более короткий как подстроку, не
был "съеден" более коротким совпадением раньше.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `body_md` | `str` | Исходный текст. |
| `titles_to_link` | `list[str]` | Заголовки, которые нужно превратить в `[[wikilink]]` при первом упоминании. |

**Возвращаемое значение:** `str` — текст с проставленными ссылками.

**Исключения:** не поднимает.

**Примечание:** судя по коду ролей (`roles/synthesizer_writer.py`), эта
функция в АКТИВНОМ пайплайне НЕ вызывается — Writer сам указывает
`links_out` явным списком в structured-ответе, а не через постфактум-простановку
по тексту; функция задокументирована как существующий skill, доступный для
будущего использования.

### 3.8. `sanitize_wikilinks(text: str) -> str`

**Описание.** Убирает случайное ДУБЛИРОВАНИЕ скобок (`[[[[X]]]] → [[X]]`),
которое иногда генерирует LLM при вложенной подстановке шаблона ссылки.
Применяется РЕКУРСИВНО (цикл `while previous != result`) на случай
тройной/четверной вложенности — не одна замена, а до стабилизации.

**Параметры:** `text: str`.
**Возвращаемое значение:** `str`. Пустая строка/`None`-подобный вход возвращается как есть.
**Исключения:** не поднимает.

**Отличие от `strip_wikilink_brackets` (важно, см. докстринг обеих функций в
коде):** `sanitize_wikilinks` СХЛОПЫВАЕТ `[[[[X]]]]` к ОДНОЙ паре скобок
`[[X]]` — полезно для ТЕЛА заметки, где `[[wikilink]]` и так должен остаться
ссылкой. `strip_wikilink_brackets` полностью УБИРАЕТ скобки — используется
там, где скобки добавляются программно (`render_markdown` при рендере
`links_out`), чтобы избежать бага `[[[[Title]]]]`, когда LLM иногда кладёт в
`links_out` уже обёрнутую строку `"[[Title]]"` вместо чистой `"Title"`, а
внешний код оборачивает её снова.

### 3.9. `strip_wikilink_brackets(text: str) -> str`

**Описание.** Полностью убирает обрамляющие `[[ ]]` у заголовка ссылки (в
цикле, пока строка начинается на `[` и заканчивается на `]`) — гарантирует,
что последующее оборачивание кодом (`f"[[{title}]]"`) произойдёт РОВНО ОДИН РАЗ.

**Параметры:** `text: str`.
**Возвращаемое значение:** `str` — заголовок без каких-либо квадратных скобок,
с обрезанными пробелами по краям. Пустой вход возвращается как есть.
**Исключения:** не поднимает.

### 3.10. `normalize_link_title(title: str) -> str`

**Описание.** Нормализует заголовок ДЛЯ СРАВНЕНИЯ ссылок с реальными файлами:
NFC-нормализация Unicode + унификация вариантов дефиса/тире через
`_DASH_VARIANTS`. НЕ используется для отображения — только чтобы понять,
ссылается ли LLM на уже существующую заметку под слегка другим написанием
дефиса (см. `validation/link_validator.py::validate_links`,
`roles/synthesizer_writer.py::_to_draft_note::_resolve_link`).

**Параметры:** `title: str`.
**Возвращаемое значение:** `str` — нормализованная форма (не для показа пользователю).
**Исключения:** не поднимает.

---

## 4. `tools/dedup.py` — skill "duplicate detection" (роль Vault Analyst)

**Назначение (из докстринга модуля).** Дизайн-решение (подтверждено
пользователем): ЛОКАЛЬНЫЕ эмбеддинги включены в MVP-0. Модель грузится
лениво и ОДИН РАЗ (`sentence-transformers`, офлайн после первого скачивания
весов). Если библиотека/модель недоступны — система НЕ падает, а логирует
предупреждение и работает в режиме keyword-only (BM25) — GRACEFUL
DEGRADATION, а не хардстоп, т.к. отсутствие эмбеддингов НЕ является
нарушением `FREE_ONLY` (это отдельная опциональная возможность).

### 4.1. `class LocalEmbedder`

**Описание.** Тонкая обёртка над `sentence-transformers`. Полностью локальная,
без сети после первой загрузки весов модели (загрузка — забота пользователя
при первом запуске, не связана с Groq/`FREE_ONLY`).

#### `__init__(self, model_name: str)`

**Параметры:** `model_name: str` — имя модели HuggingFace
(из `settings.embedding_model`, дефолт `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"`).
**Возвращаемое значение:** — (конструктор, НЕ грузит модель сразу — `self._model = None`).

#### `_ensure_loaded(self)` (приватный)

**Описание.** Лениво загружает модель при ПЕРВОМ обращении
(`from sentence_transformers import SentenceTransformer` — локальный импорт,
модуль не требует пакета, если не используется). Кэширует в `self._model`.

**Возвращаемое значение:** объект `SentenceTransformer`.
**Исключения:** `ImportError` (пакет не установлен), `OSError`
(нет сети для первого скачивания модели) — НЕ перехватываются здесь (перехват
происходит выше, в `try_create_embedder`, см. §4.3).

#### `embed(self, text: str) -> list[float]`

**Описание.** Кодирует ОДИН текст в нормализованный вектор
(`model.encode(text, normalize_embeddings=True)`).

**Параметры:** `text: str`.
**Возвращаемое значение:** `list[float]`.
**Исключения:** пробрасывает всё, что поднимет `_ensure_loaded()`/`model.encode(...)`.

#### `embed_batch(self, texts: list[str]) -> list[list[float]]`

**Описание.** Батчевое кодирование нескольких текстов за один вызов модели
(эффективнее, чем `embed` в цикле, за счёт векторизации внутри `sentence-transformers`).

**Параметры:** `texts: list[str]`.
**Возвращаемое значение:** `list[list[float]]`.
**Исключения:** те же, что у `embed`.

### 4.2. `try_create_embedder(model_name: str, enabled: bool) -> LocalEmbedder | None`

**Описание.** Единственная безопасная точка создания эмбеддера в системе.
Если `enabled=False` — сразу `None` (пользователь/настройка явно отключили).
Иначе создаёт `LocalEmbedder`, СРАЗУ вызывает `_ensure_loaded()` — намеренный
"fail fast здесь, а не в середине индексации" (см. докстринг метода в коде):
лучше узнать о недоступности эмбеддингов один раз при старте, чем посреди
долгого прохода по Vault. Любое исключение (`ImportError`, `OSError` при
отсутствии сети и т.п.) перехватывается, логируется
(`logger.warning("Локальные эмбеддинги недоступны (%s). Переходим на
keyword-only (BM25) retrieval.", exc)`), возвращается `None`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `model_name` | `str` | Имя модели. Из `settings.embedding_model`. |
| `enabled` | `bool` | Из `settings.use_local_embeddings`. |

**Возвращаемое значение:** `LocalEmbedder | None`.

**Исключения:** НЕ поднимает — все сбои перехвачены и превращены в `None` + лог.

**Кто вызывает:** `cli/main.py::approve`/`index`, `orchestrator/state_machine.py::Orchestrator.__init__`.

### 4.3. `cosine_similarity(a: list[float], b: list[float]) -> float`

**Описание.** Классическая косинусная схожесть двух векторов, реализована
БЕЗ `numpy` (чистый Python, `sum`/`math.sqrt`) — минимизация зависимостей
для такой простой операции.

**Параметры:** `a: list[float]`, `b: list[float]`.

**Возвращаемое значение:** `float` в `[-1, 1]` (на практике для нормализованных
эмбеддингов — `[0, 1]`). Возвращает `0.0`, если любой из векторов пуст, длины
не совпадают, или норма любого из них равна `0` (защита от деления на ноль).

**Исключения:** не поднимает.

### 4.4. `classify_similarity(score: float, high: float, low: float) -> str`

**Описание.** Трёхуровневая классификация схожести (используется
`roles/vault_analyst.py::resolve_notes_against_vault`, см.
`docs_roles_part2.md §2`):
- `score >= high` → `"duplicate"` — детерминированно считаем той же
  концепцией, LLM НЕ нужен;
- `low <= score < high` → `"ambiguous"` — "серая зона", отдаётся на ОДИН
  маленький LLM-вызов (`roles/vault_analyst.py::_resolve_ambiguous`);
- `score < low` → `"distinct"` — точно разные концепции, LLM не нужен.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `score` | `float` | Итоговый скор схожести (`RetrievalHit.combined_score` — комбинация BM25 и эмбеддингов, см. `retrieval/search.py`). |
| `high` | `float` | Порог "точно дубликат". Из `settings.dedup_high_threshold` (дефолт `0.85`). |
| `low` | `float` | Порог "точно разные". Из `settings.dedup_low_threshold` (дефолт `0.55`). |

**Возвращаемое значение:** `str` — одно из `"duplicate"`/`"ambiguous"`/`"distinct"`.

**Исключения:** не поднимает.

---

## Сводная таблица: какие роли используют какие skills из `tools/`

| Skill (`tools/*.py`) | Функции | Используется в роли |
|---|---|---|
| `web_search.py` | `search_web`, `deduplicate_by_url` | `roles/researcher.py::collect_raw_candidates` (только `RESEARCH_MODE=web`) |
| `web_fetch.py` | `fetch_clean_text` | `roles/researcher.py::fetch_selected_sources` (только `RESEARCH_MODE=web`) |
| `markdown_tools.py` | `build_note_path`, `render_markdown`, `sanitize_wikilinks`, `strip_wikilink_brackets`, `normalize_link_title`, `slugify_filename` | `roles/synthesizer_writer.py`, `vault/writer.py`, `staging/changeset.py`, `staging/draft_merge.py`, `orchestrator/state_machine.py` (топик-папка через `slugify_filename`) |
| `dedup.py` | `try_create_embedder`, `cosine_similarity`, `classify_similarity`, `LocalEmbedder` | `roles/vault_analyst.py`, `retrieval/search.py::VaultSearcher`, `vault/index.py::VaultIndexer`, `cli/main.py` |

Документация по `tools/` завершена.
