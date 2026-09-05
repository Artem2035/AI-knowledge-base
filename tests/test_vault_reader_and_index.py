from __future__ import annotations

from pathlib import Path

import pytest

from vault.db import VaultDB
from vault.index import VaultIndexer
from vault.reader import read_vault

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "test_vault"


def test_read_vault_parses_notes():
    notes = read_vault(FIXTURE_VAULT)
    titles = {n.title for n in notes}
    assert "Эмбеддинги" in titles
    assert "Векторные базы данных" in titles
    assert "Python основы" in titles


def test_read_vault_extracts_frontmatter_and_tags():
    notes = read_vault(FIXTURE_VAULT)
    emb = next(n for n in notes if n.title == "Эмбеддинги")
    assert "ml" in emb.tags
    assert "embeddings" in emb.tags
    assert emb.frontmatter.get("title") == "Эмбеддинги"


def test_read_vault_extracts_wikilinks():
    notes = read_vault(FIXTURE_VAULT)
    vdb = next(n for n in notes if n.title == "Векторные базы данных")
    assert "Эмбеддинги" in vdb.outlinks


def test_indexer_sync_is_incremental(tmp_path):
    db_path = tmp_path / "index.sqlite3"
    db = VaultDB(db_path)
    indexer = VaultIndexer(db=db, vault_path=FIXTURE_VAULT, embedder=None)

    stats1 = indexer.sync()
    assert stats1["scanned"] == 3
    assert stats1["updated"] == 3
    assert stats1["unchanged"] == 0

    stats2 = indexer.sync()
    assert stats2["updated"] == 0
    assert stats2["unchanged"] == 3

    db.close()


def test_indexer_resolve_wikilink_targets(tmp_path):
    db_path = tmp_path / "index.sqlite3"
    db = VaultDB(db_path)
    indexer = VaultIndexer(db=db, vault_path=FIXTURE_VAULT, embedder=None)
    indexer.sync()
    indexer.resolve_wikilink_targets()

    backlinks = db.get_backlinks("Знания/Эмбеддинги.md")
    assert "Знания/Векторные базы данных.md" in backlinks
    db.close()


def test_missing_vault_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_vault(tmp_path / "does_not_exist")
