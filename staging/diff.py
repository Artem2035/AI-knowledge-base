from __future__ import annotations

from storage.models import StagingChangeset, DraftNote


_DOMAIN_TAG_PREFIX = "domain/"


def _domain_of(d: DraftNote) -> str:
    """Домен заметки из тега domain/<домен> (его добавляет код при сборке)."""
    for tag in d.tags:
        if tag.startswith(_DOMAIN_TAG_PREFIX):
            return tag[len(_DOMAIN_TAG_PREFIX):]
    return ""


def _note_markers(d: DraftNote) -> list[str]:
    """Пометки для заметки: knowledge-режим (нет проверяемых источников),
    разделы, которые модель пометила как неточные, и (legacy) незавершённое
    критик-ревью. Все сигналы нужны пользователю ДО approve."""
    markers: list[str] = []
    if d.frontmatter.get("source") == "model-knowledge":
        markers.append("⚠ без внешних источников (конспект по знаниям модели)")
    if d.unverified_sections:
        markers.append("⚠ разделы требуют проверки: " + ", ".join(d.unverified_sections))
    if d.needs_review:  # legacy: Critic удалён, поле осталось для старых changeset.json
        markers.append(
            f"⚠ критик не одобрил после {d.critic_rounds} попыт(ки/ок) переписывания — проверьте вручную"
        )
    return markers


def render_diff_summary(changeset: StagingChangeset) -> str:
    lines: list[str] = []
    lines.append(f"Задача: {changeset.task_id}")
    lines.append("")

    if changeset.creates:
        lines.append(f"НОВЫЕ ЗАМЕТКИ ({len(changeset.creates)}):")
        for d in changeset.creates:
            tags = ", ".join(t for t in d.tags if not t.startswith(_DOMAIN_TAG_PREFIX)) or "—"
            lines.append(f"  + {d.path}")
            lines.append(f"      заголовок: {d.title}")
            domain = _domain_of(d)
            if domain:
                lines.append(f"      домен: {domain}")
            lines.append(f"      теги: {tags}")
            if d.links_out:
                lines.append(f"      связи: {', '.join(f'[[{t}]]' for t in d.links_out)}")
            for marker in _note_markers(d):
                lines.append(f"      {marker}")
        lines.append("")

    if changeset.updates:
        lines.append(f"ДОПОЛНЯЕМЫЕ ЗАМЕТКИ ({len(changeset.updates)}):")
        for d in changeset.updates:
            lines.append(f"  ~ {d.path}")
            if d.append_section:
                preview = d.append_section.strip().splitlines()[0][:80]
                lines.append(f"      добавляется секция, начинается с: {preview}…")
            for marker in _note_markers(d):
                lines.append(f"      {marker}")
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