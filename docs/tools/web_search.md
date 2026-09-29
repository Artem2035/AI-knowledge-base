# Документация: `tools/web_search.py` — веб-поиск (только `RESEARCH_MODE=web`)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Только бесплатный DuckDuckGo через `ddgs`: без ключей, без платных провайдеров, без fallback на Tavily/SerpAPI (следствие `FREE_ONLY`).

## `search_web(query: str, max_results: int = 6, subtopic: str = "") -> list[SourceCandidate]`

Возвращает СЫРЫЕ кандидаты; отбор релевантности — отдельный LLM-шаг в `roles/researcher.py`. Импорт `ddgs` — локальный (модуль не требует пакета, пока не используется).

| Имя | Тип | Назначение |
|---|---|---|
| `query` | `str` | Поисковый запрос. |
| `max_results` | `int` | Из `settings.max_search_results_per_query` (`../config/settings.md §11`). |
| `subtopic` | `str` | Проставляется в `SourceCandidate.subtopic`. |

**Возвращает:** `SourceCandidate` (`../storage/models.md`) с `url` (`href`), `title`, `snippet` (`body`), `subtopic`.
**Исключения:** `RuntimeError`, если `ddgs` не установлен. Сетевые сбои НЕ пробрасываются: логируются (`warning`), возвращается то, что успело собраться (возможно `[]`).

## `deduplicate_by_url(candidates: list[SourceCandidate]) -> list[SourceCandidate]`

Убирает дубли по `url.rstrip("/").lower()`, сохраняя первое вхождение и порядок; пустые URL пропускаются. Не поднимает исключений.

Документация по `tools/web_search.py` завершена. Далее — `web_fetch.md`.
