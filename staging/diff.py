from __future__ import annotations

from storage.models import StagingChangeset


def render_diff_summary(changeset: StagingChangeset) -> str:
    lines: list[str] = []
    lines.append(f"Задача: {changeset.task_id}")
    lines.append("")

    if changeset.creates:
        lines.append(f"НОВЫЕ ЗАМЕТКИ ({len(changeset.creates)}):")
        for d in changeset.creates:
            tags = ", ".join(d.tags) if d.tags else "—"
            lines.append(f"  + {d.path}")
            lines.append(f"      заголовок: {d.title}")
            lines.append(f"      теги: {tags}")
            if d.links_out:
                lines.append(f"      связи: {', '.join(f'[[{t}]]' for t in d.links_out)}")
        lines.append("")

    if changeset.updates:
        lines.append(f"ДОПОЛНЯЕМЫЕ ЗАМЕТКИ ({len(changeset.updates)}):")
        for d in changeset.updates:
            lines.append(f"  ~ {d.path}")
            if d.append_section:
                preview = d.append_section.strip().splitlines()[0][:80]
                lines.append(f"      добавляется секция, начинается с: {preview}…")
        lines.append("")

    if changeset.deletes:
        lines.append(f"⚠️  УДАЛЕНИЯ ({len(changeset.deletes)}) — по умолчанию заблокированы:")
        for p in changeset.deletes:
            lines.append(f"  - {p}")
        lines.append("")

    if changeset.relationships:
        lines.append(f"Новые связи (wikilinks): {len(changeset.relationships)}")
        lines.append("")

    if changeset.validation:
        v = changeset.validation
        status = "OK" if v.ok else "ЕСТЬ ОШИБКИ"
        lines.append(f"Валидация: {status} (errors={len(v.errors)}, warnings={len(v.warnings)})")
        for issue in v.issues:
            marker = "❌" if issue.level == "error" else "⚠️"
            lines.append(f"  {marker} [{issue.code}] {issue.message}")

    return "\n".join(lines)
