"""
SQLite-индекс Vault. Это НЕ дублирование Vault, а лёгкий локальный кэш
метаданных для быстрого retrieval без парсинга всех файлов на каждый запрос
и без отправки всего Vault в Gemini.

Схема:
    notes(path, title, content_hash, raw_content, summary, created_at, updated_at)
    frontmatter(note_path, key, value_json)
    tags(note_path, tag)
    links(source_path, target_path, link_type)
    embeddings(note_path, model, vector_json)   -- опционально
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS frontmatter (
    note_path TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    FOREIGN KEY (note_path) REFERENCES notes(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_frontmatter_note ON frontmatter(note_path);

CREATE TABLE IF NOT EXISTS tags (
    note_path TEXT NOT NULL,
    tag TEXT NOT NULL,
    FOREIGN KEY (note_path) REFERENCES notes(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_note ON tags(note_path);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS links (
    source_path TEXT NOT NULL,
    target_path TEXT,
    target_title TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'wikilink'
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_path);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_path);

CREATE TABLE IF NOT EXISTS embeddings (
    note_path TEXT NOT NULL,
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    PRIMARY KEY (note_path, model)
);
"""


class VaultDB:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "VaultDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- notes ---------------------------------------------------------

    def upsert_note(
        self,
        path: str,
        title: str,
        content_hash: str,
        raw_content: str,
        summary: str,
        frontmatter: dict[str, Any],
        tags: Iterable[str],
        created_at: str,
        updated_at: str,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO notes (path, title, content_hash, raw_content, summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title,
                content_hash=excluded.content_hash,
                raw_content=excluded.raw_content,
                summary=excluded.summary,
                updated_at=excluded.updated_at
            """,
            (path, title, content_hash, raw_content, summary, created_at, updated_at),
        )
        cur.execute("DELETE FROM frontmatter WHERE note_path = ?", (path,))
        for k, v in frontmatter.items():
            cur.execute(
                "INSERT INTO frontmatter (note_path, key, value_json) VALUES (?, ?, ?)",
                (path, k, json.dumps(v, ensure_ascii=False, default=str)),
            )
        cur.execute("DELETE FROM tags WHERE note_path = ?", (path,))
        for t in tags:
            cur.execute("INSERT INTO tags (note_path, tag) VALUES (?, ?)", (path, t))
        self.conn.commit()

    def note_exists_with_hash(self, path: str, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notes WHERE path = ? AND content_hash = ?", (path, content_hash)
        ).fetchone()
        return row is not None

    def get_all_notes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM notes").fetchall()

    def get_note(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()

    def delete_note(self, path: str) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM notes WHERE path = ?", (path,))
        cur.execute("DELETE FROM frontmatter WHERE note_path = ?", (path,))
        cur.execute("DELETE FROM tags WHERE note_path = ?", (path,))
        cur.execute("DELETE FROM links WHERE source_path = ?", (path,))
        cur.execute("DELETE FROM embeddings WHERE note_path = ?", (path,))
        self.conn.commit()

    def get_all_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM notes").fetchall()}

    # -- links -----------------------------------------------------------

    def set_links(self, source_path: str, links: list[tuple[str | None, str, str]]) -> None:
        """links: список (target_path_or_None, target_title, link_type)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM links WHERE source_path = ?", (source_path,))
        for target_path, target_title, link_type in links:
            cur.execute(
                "INSERT INTO links (source_path, target_path, target_title, link_type) VALUES (?, ?, ?, ?)",
                (source_path, target_path, target_title, link_type),
            )
        self.conn.commit()

    def get_backlinks(self, target_path: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT source_path FROM links WHERE target_path = ?", (target_path,)
        ).fetchall()
        return [r["source_path"] for r in rows]

    def get_outlinks(self, source_path: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT target_title FROM links WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [r["target_title"] for r in rows]

    # -- tags --------------------------------------------------------------

    def get_all_tags(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT tag FROM tags").fetchall()
        return sorted(r["tag"] for r in rows)

    # -- embeddings ----------------------------------------------------

    def set_embedding(self, note_path: str, model: str, vector: list[float]) -> None:
        self.conn.execute(
            """
            INSERT INTO embeddings (note_path, model, vector_json) VALUES (?, ?, ?)
            ON CONFLICT(note_path, model) DO UPDATE SET vector_json=excluded.vector_json
            """,
            (note_path, model, json.dumps(vector)),
        )
        self.conn.commit()

    def get_embedding(self, note_path: str, model: str) -> list[float] | None:
        row = self.conn.execute(
            "SELECT vector_json FROM embeddings WHERE note_path = ? AND model = ?",
            (note_path, model),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["vector_json"])

    def get_all_embeddings(self, model: str) -> dict[str, list[float]]:
        rows = self.conn.execute(
            "SELECT note_path, vector_json FROM embeddings WHERE model = ?", (model,)
        ).fetchall()
        return {r["note_path"]: json.loads(r["vector_json"]) for r in rows}
