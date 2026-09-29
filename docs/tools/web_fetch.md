# Документация: `tools/web_fetch.py` — загрузка и очистка страниц (только `RESEARCH_MODE=web`)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Бесплатно: `httpx` (HTTP) + `trafilatura` (чистый текст без рекламы/навигации).

## Константы

| Имя | Значение | Назначение |
|---|---|---|
| `USER_AGENT` | `"Mozilla/5.0 (compatible; ObsidianAIKB/0.1; personal-use-research-bot)"` | Честно идентифицирует бота, не маскируется под браузер. |
| `MAX_CHARS_PER_SOURCE` | `12000` | Потолок текста одного источника — не раздувать контекст LLM. |

## `fetch_clean_text(url: str, timeout: float = 15.0) -> tuple[str | None, str | None]`

Скачивает страницу (`follow_redirects=True`, `raise_for_status()`), извлекает текст `trafilatura.extract(html, include_comments=False, include_tables=False)`, обрезает до `MAX_CHARS_PER_SOURCE`.

**Возвращает `(text, error)`** — ровно один непуст:
- успех: `(text, None)`;
- HTTP-сбой: `(None, "fetch_error: ...")`;
- сбой извлечения: `(None, "extract_error: ...")`;
- пустой результат: `(None, "extract_error: empty content")`.

**Исключения:** не поднимает — все сбои превращены в второй элемент кортежа.

Документация по `tools/web_fetch.py` завершена. Обзор — `_index.md`.
