from __future__ import annotations

from storage.models import DraftNote, ValidationIssue
from vault.db import VaultDB


def validate_links(
    drafts: list[DraftNote], db: VaultDB
) -> list[ValidationIssue]:
    """
    Проверяет [[wikilink]]-цели: существуют ли они либо в реальном Vault
    (через индекс), либо среди других заметок в этом же changeset.
    Broken link — это не ошибка (Obsidian поддерживает "красные" ссылки на
    будущие заметки), но мы предупреждаем, чтобы пользователь видел это
    явно перед approve.
    """
    issues: list[ValidationIssue] = []
    existing_titles = {row["title"] for row in db.get_all_notes()}
    draft_titles = {d.title for d in drafts}
    known_titles = existing_titles | draft_titles

    for d in drafts:
        for linked_title in d.links_out:
            if linked_title not in known_titles:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="broken_wikilink",
                        message=f"Заметка '{d.title}' ссылается на несуществующую заметку "
                        f"'{linked_title}' (будет создана 'красная' ссылка в Obsidian)",
                        draft_id=d.draft_id,
                    )
                )

    return issues


def validate_no_path_collisions(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    existing_paths = db.get_all_paths()
    seen_in_batch: dict[str, str] = {}

    for d in drafts:
        if d.action.value == "create" and d.path in existing_paths:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="create_collides_with_existing",
                    message=f"action=create, но файл уже существует в Vault: {d.path}. "
                    "Должно было быть action=update.",
                    draft_id=d.draft_id,
                )
            )
        if d.action.value == "update" and d.path not in existing_paths:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="update_missing_target",
                    message=f"action=update ссылается на несуществующий файл: {d.path}",
                    draft_id=d.draft_id,
                )
            )
        if d.path in seen_in_batch:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="duplicate_path_in_changeset",
                    message=f"Два черновика в одном changeset нацелены на один и тот же путь: {d.path}",
                    draft_id=d.draft_id,
                )
            )
        seen_in_batch[d.path] = d.draft_id

    return issues
