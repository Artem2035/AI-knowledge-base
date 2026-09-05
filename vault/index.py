"""
Построение и инкрементальное обновление локального индекса Vault.

Инкрементальность: если для файла уже есть запись в notes с тем же
content_hash — файл не перепарсивается, эмбеддинг не пересчитывается.
Это то, что позволяет НЕ пересканировать весь Vault на каждый запрос.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from vault.db import VaultDB
from vault.reader import ParsedNote, _make_summary, read_vault

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VaultIndexer:
    def __init__(self, db: VaultDB, vault_path: Path, embedder=None):
        self.db = db
        self.vault_path = vault_path
        self.embedder = embedder  # tools.dedup.LocalEmbedder | None

    def sync(self) -> dict:
        """
        Полный проход по Vault с инкрементальным апдейтом индекса.
        Возвращает статистику: {"scanned", "updated", "unchanged", "removed"}.
        """
        notes = read_vault(self.vault_path)
        current_paths = {n.path for n in notes}
        existing_paths = self.db.get_all_paths()

        updated = 0
        unchanged = 0
        for note in notes:
            if self.db.note_exists_with_hash(note.path, note.content_hash):
                unchanged += 1
                continue
            self._upsert(note)
            updated += 1

        removed_paths = existing_paths - current_paths
        for path in removed_paths:
            # Заметка удалена/перемещена вне системы (пользователь мог убрать её
            # руками в Obsidian) — просто убираем из индекса. Это НЕ удаление
            # файла (файла уже нет), поэтому ALLOW_DELETE тут не применим.
            self.db.delete_note(path)

        return {
            "scanned": len(notes),
            "updated": updated,
            "unchanged": unchanged,
            "removed": len(removed_paths),
        }

    def _upsert(self, note: ParsedNote) -> None:
        summary = _make_summary(note.body)
        self.db.upsert_note(
            path=note.path,
            title=note.title,
            content_hash=note.content_hash,
            raw_content=note.raw_content,
            summary=summary,
            frontmatter=note.frontmatter,
            tags=note.tags,
            created_at=_now(),
            updated_at=_now(),
        )
        links = [(None, title, "wikilink") for title in note.outlinks]
        self.db.set_links(note.path, links)

        if self.embedder is not None:
            try:
                vector = self.embedder.embed(f"{note.title}\n{summary}")
                self.db.set_embedding(note.path, self.embedder.model_name, vector)
            except Exception as exc:  # эмбеддинги — best-effort, не должны ломать индексацию
                logger.warning("Embedding failed for %s: %s", note.path, exc)

    def resolve_wikilink_targets(self) -> None:
        """
        Второй проход: сопоставить target_title -> target_path там, где
        заметка с таким заголовком существует в индексе (для backlinks).
        """
        title_to_path: dict[str, str] = {}
        for row in self.db.get_all_notes():
            title_to_path[row["title"]] = row["path"]

        for row in self.db.get_all_notes():
            outlink_titles = self.db.get_outlinks(row["path"])
            resolved = [
                (title_to_path.get(t), t, "wikilink") for t in outlink_titles
            ]
            if resolved:
                self.db.set_links(row["path"], resolved)
