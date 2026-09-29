# Документация: `tools/dedup.py` — обнаружение дубликатов и эмбеддинги

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Локальные эмбеддинги включены в MVP; модель грузится лениво и один раз (`sentence-transformers`, офлайн после первого скачивания). Если библиотека/модель недоступна — система НЕ падает, а переходит на keyword-only (BM25): это graceful degradation, не нарушение `FREE_ONLY`.

## `class LocalEmbedder`

Тонкая обёртка над `sentence-transformers`, без сети после первой загрузки весов.

- `__init__(self, model_name: str)` — имя из `settings.embedding_model` (`../config/settings.md §8`); модель НЕ грузится сразу.
- `_ensure_loaded(self)` — ленивая загрузка при первом обращении (локальный импорт). Поднимает `ImportError`/`OSError` — перехватываются выше, в `try_create_embedder`.
- `embed(self, text) -> list[float]` — нормализованный вектор.
- `embed_batch(self, texts) -> list[list[float]]` — батч за один вызов модели.

## `try_create_embedder(model_name: str, enabled: bool) -> LocalEmbedder | None`

Единственная безопасная точка создания. `enabled=False` → `None`. Иначе создаёт и СРАЗУ вызывает `_ensure_loaded()` (fail fast при старте, а не посреди индексации). Любое исключение → `logger.warning(...)` + `None`. **Не поднимает.** Вызывают `Orchestrator.__init__`, `cli/main.py::approve`/`index`.

## `cosine_similarity(a, b) -> float`

Чистый Python (без `numpy`). Пустые векторы, разная длина или нулевая норма → `0.0`.

## `classify_similarity(score: float, high: float, low: float) -> str`

- `score >= high` → `"duplicate"` (LLM не нужен);
- `low <= score < high` → `"ambiguous"` (серая зона — один LLM-вызов, `roles/vault_analyst.py::_resolve_ambiguous`);
- `score < low` → `"distinct"`.

Пороги — `settings.dedup_high_threshold` (0.85) / `dedup_low_threshold` (0.55). `score` — `RetrievalHit.combined_score` из `retrieval/search.py`. Не поднимает.

Документация по `tools/dedup.py` завершена. Пакет закрыт — обзор `_index.md`.
