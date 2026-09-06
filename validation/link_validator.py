from __future__ import annotations

from storage.models import DraftNote, ValidationIssue
from tools.markdown_tools import normalize_link_title
from vault.db import VaultDB


def validate_links(drafts: list[DraftNote], db: VaultDB) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    existing_titles = {row["title"] for row in db.get_all_notes()}
    draft_titles = {d.title for d in drafts}
    known_titles = existing_titles | draft_titles
    known_normalized = {normalize_link_title(t): t for t in known_titles}

    for d in drafts:
        for linked_title in d.links_out:
            if linked_title in known_titles:
                continue
            normalized = normalize_link_title(linked_title)
            if normalized in known_normalized:
                # Совпадает после нормализации дефисов — это, скорее всего,
                # то же самое ссылка, но synthesizer_writer.py уже должен
                # был снапнуть её к точному имени. Если сюда всё же дошло —
                # это баг снаппинга, а не намеренный "красный" wikilink.
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="wikilink_dash_variant_mismatch",
                        message=(
                            f"Заметка '{d.title}' ссылается на '{linked_title}', "
                            f"что почти совпадает с существующей '{known_normalized[normalized]}' "
                            "(отличаются только Unicode-варианты дефиса). Проверьте вручную."
                        ),
                        draft_id=d.draft_id,
                    )
                )
                continue
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
