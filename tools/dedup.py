"""
Skill "duplicate detection" (Vault Analyst).

Дизайн-решение (подтверждено пользователем): локальные эмбеддинги включены
в MVP-0. Модель грузится лениво и один раз (sentence-transformers, офлайн
после первого скачивания весов). Если библиотека/модель недоступны —
система НЕ падает, а логирует предупреждение и работает в режиме
keyword-only (BM25) — graceful degradation, а не хардстоп, т.к. отсутствие
эмбеддингов не является нарушением FREE_ONLY (это отдельная опциональная
возможность).
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class LocalEmbedder:
    """Тонкая обёртка над sentence-transformers. Полностью локальная, без сети
    после первой загрузки весов модели (загрузка — забота пользователя при
    первом запуске, не связана с Gemini/FREE_ONLY)."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # локальный импорт

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._ensure_loaded()
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_loaded()
        vectors = model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vectors]


def try_create_embedder(model_name: str, enabled: bool) -> LocalEmbedder | None:
    if not enabled:
        return None
    try:
        embedder = LocalEmbedder(model_name)
        embedder._ensure_loaded()  # fail fast здесь, а не в середине индексации
        return embedder
    except Exception as exc:  # ImportError, OSError при отсутствии сети для скачивания модели и т.д.
        logger.warning(
            "Локальные эмбеддинги недоступны (%s). Переходим на keyword-only (BM25) retrieval.",
            exc,
        )
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def classify_similarity(score: float, high: float, low: float) -> str:
    """
    high  -> "duplicate"  — детерминированно считаем той же концепцией, LLM не нужен
    low..high -> "ambiguous" — "серая зона", отдаётся на 1 маленький Gemini-вызов
    <low  -> "distinct" — точно разные концепции
    """
    if score >= high:
        return "duplicate"
    if score >= low:
        return "ambiguous"
    return "distinct"
