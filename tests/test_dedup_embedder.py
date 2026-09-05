from __future__ import annotations

from tools.dedup import try_create_embedder


def test_try_create_embedder_returns_none_on_missing_dependency(monkeypatch):
    """
    Если sentence-transformers не установлен (или модель не может
    загрузиться, напр. нет сети для первого скачивания весов), система
    должна тихо перейти на keyword-only retrieval, а не упасть.
    """
    embedder = try_create_embedder("nonexistent/model-name-xyz", enabled=True)
    assert embedder is None


def test_try_create_embedder_returns_none_when_disabled():
    embedder = try_create_embedder("any/model", enabled=False)
    assert embedder is None
