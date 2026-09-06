"""
Единственная точка записи в РЕАЛЬНЫЙ Vault.

КРИТИЧЕСКОЕ ПРАВИЛО ПРОЕКТА: этот модуль вызывается только из
staging/commit-логики, и только после явного approve пользователя в CLI.
Ничего в roles/*, gemini/* или retrieval/* не должно импортировать этот
модуль напрямую.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from storage.models import DraftNote, NoteAction
from tools.markdown_tools import render_markdown, sanitize_wikilinks


@dataclass
class WriteResult:
    path: str
    action: str
    backup_of: str | None = None  # путь к snapshot "before", если это UPDATE


class VaultWriter:
    def __init__(self, vault_path: Path, allow_delete: bool = False):
        self.vault_path = vault_path
        self.allow_delete = allow_delete

    def write_draft(self, draft: DraftNote, backup_dir: Path | None = None) -> WriteResult:
        full_path = self.vault_path / draft.path
        backup_of = None

        if draft.action == NoteAction.CREATE:
            if full_path.exists():
                raise FileExistsError(
                    f"Попытка создать заметку, которая уже существует: {draft.path}. "
                    "Это должно было быть отловлено Validator-ом раньше."
                )
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(render_markdown(draft), encoding="utf-8")

        elif draft.action == NoteAction.UPDATE:
            if not full_path.exists():
                raise FileNotFoundError(
                    f"Попытка обновить несуществующую заметку: {draft.path}"
                )
            if backup_dir is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / draft.path.replace("/", "__")
                backup_path.write_text(full_path.read_text(encoding="utf-8"), encoding="utf-8")
                backup_of = str(backup_path)

            if draft.append_section:
                existing = full_path.read_text(encoding="utf-8")
                new_content = existing.rstrip() + "\n\n" + sanitize_wikilinks(draft.append_section.strip()) + "\n"
                full_path.write_text(new_content, encoding="utf-8")
            else:
                full_path.write_text(render_markdown(draft), encoding="utf-8")

        return WriteResult(path=draft.path, action=draft.action.value, backup_of=backup_of)

    def delete_note(self, rel_path: str) -> None:
        if not self.allow_delete:
            raise PermissionError(
                "Удаление заметок запрещено (ALLOW_DELETE=false). "
                "Это ограничение по умолчанию и намеренно не обходится автоматически."
            )
        full_path = self.vault_path / rel_path
        if full_path.exists():
            full_path.unlink()

    def ensure_folder(self, rel_folder: str) -> None:
        (self.vault_path / rel_folder).mkdir(parents=True, exist_ok=True)
