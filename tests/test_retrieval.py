from __future__ import annotations

from pathlib import Path

from retrieval.search import VaultSearcher
from tools.dedup import classify_similarity, cosine_similarity
from vault.db import VaultDB
from vault.index import VaultIndexer

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "test_vault"


def test_bm25_search_finds_relevant_note(tmp_path):
    db = VaultDB(tmp_path / "index.sqlite3")
    VaultIndexer(db=db, vault_path=FIXTURE_VAULT, embedder=None).sync()

    searcher = VaultSearcher(db, embedder=None)
    hits = searcher.search("эмбеддинги векторное представление текста", top_k=3)

    assert hits, "поиск должен вернуть хотя бы один результат"
    assert hits[0].title == "Эмбеддинги"
    db.close()


def test_bm25_search_no_results_on_empty_index(tmp_path):
    db = VaultDB(tmp_path / "index.sqlite3")
    searcher = VaultSearcher(db, embedder=None)
    assert searcher.search("что угодно") == []
    db.close()


def test_cosine_similarity_basic():
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert cosine_similarity([], [1]) == 0.0


def test_classify_similarity_thresholds():
    assert classify_similarity(0.9, high=0.85, low=0.55) == "duplicate"
    assert classify_similarity(0.7, high=0.85, low=0.55) == "ambiguous"
    assert classify_similarity(0.2, high=0.85, low=0.55) == "distinct"
