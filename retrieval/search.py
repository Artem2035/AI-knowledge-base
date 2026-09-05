"""
Локальный поиск по индексу Vault (SQLite). В Gemini передаются только
title+summary+frontmatter top-N кандидатов — никогда весь текст всех заметок.
"""
from __future__ import annotations

from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from tools.dedup import LocalEmbedder, cosine_similarity
from vault.db import VaultDB


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in text.replace("/", " ").replace("-", " ").split() if t.strip()]


@dataclass
class RetrievalHit:
    path: str
    title: str
    summary: str
    tags: list[str]
    frontmatter: dict
    bm25_score: float = 0.0
    embedding_score: float | None = None

    @property
    def combined_score(self) -> float:
        if self.embedding_score is None:
            return self.bm25_score
        # нормализуем bm25 в [0,1] грубо через насыщение — комбинируем с эмбеддингом,
        # эмбеддинг важнее для семантического сходства формулировок
        bm25_norm = min(self.bm25_score / 10.0, 1.0)
        return 0.4 * bm25_norm + 0.6 * self.embedding_score


class VaultSearcher:
    def __init__(self, db: VaultDB, embedder: LocalEmbedder | None = None):
        self.db = db
        self.embedder = embedder
        self._bm25: BM25Okapi | None = None
        self._corpus_paths: list[str] = []
        self._notes_cache: dict[str, dict] = {}
        self._build_index()

    def _build_index(self) -> None:
        rows = self.db.get_all_notes()
        tokenized_corpus = []
        self._corpus_paths = []
        self._notes_cache = {}
        for row in rows:
            tags = [
                t["tag"]
                for t in self.db.conn.execute(
                    "SELECT tag FROM tags WHERE note_path = ?", (row["path"],)
                ).fetchall()
            ]
            fm_rows = self.db.conn.execute(
                "SELECT key, value_json FROM frontmatter WHERE note_path = ?", (row["path"],)
            ).fetchall()
            import json

            fm = {r["key"]: json.loads(r["value_json"]) for r in fm_rows}

            text_for_index = f"{row['title']} {row['summary']} {' '.join(tags)}"
            tokenized_corpus.append(_tokenize(text_for_index))
            self._corpus_paths.append(row["path"])
            self._notes_cache[row["path"]] = {
                "title": row["title"],
                "summary": row["summary"],
                "tags": tags,
                "frontmatter": fm,
            }

        self._bm25 = BM25Okapi(tokenized_corpus) if tokenized_corpus else None

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        if self._bm25 is None or not self._corpus_paths:
            return []

        scores = self._bm25.get_scores(_tokenize(query))
        scored = list(zip(self._corpus_paths, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[: max(top_k * 3, top_k)]  # берём с запасом до объединения с эмбеддингами

        query_vec = None
        if self.embedder is not None:
            try:
                query_vec = self.embedder.embed(query)
            except Exception:
                query_vec = None

        hits: list[RetrievalHit] = []
        for path, bm25_score in top:
            info = self._notes_cache[path]
            emb_score = None
            if query_vec is not None:
                note_vec = self.db.get_embedding(path, self.embedder.model_name)
                if note_vec is not None:
                    emb_score = cosine_similarity(query_vec, note_vec)
            hits.append(
                RetrievalHit(
                    path=path,
                    title=info["title"],
                    summary=info["summary"],
                    tags=info["tags"],
                    frontmatter=info["frontmatter"],
                    bm25_score=float(bm25_score),
                    embedding_score=emb_score,
                )
            )

        hits.sort(key=lambda h: h.combined_score, reverse=True)
        return hits[:top_k]
