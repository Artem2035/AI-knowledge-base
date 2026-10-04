from __future__ import annotations

from staging.diff import render_diff_summary
from storage.models import DraftNote, NoteAction, StagingChangeset


def _create(**kw) -> DraftNote:
    base = dict(action=NoteAction.CREATE, path="Знания/N.md", title="N", body_md="Текст.")
    base.update(kw)
    return DraftNote(**base)


def test_diff_shows_domain_hides_domain_tag_and_lists_unverified_sections():
    draft = _create(
        tags=["rag", "domain/technical"], unverified_sections=["Параметры", "Примеры"],
        frontmatter={"source": "model-knowledge"},
    )
    out = render_diff_summary(StagingChangeset(task_id="d1", creates=[draft]))

    assert "домен: technical" in out
    assert "теги: rag" in out and "domain/technical" not in out
    assert "разделы требуют проверки: Параметры, Примеры" in out
    assert "без внешних источников" in out


def test_diff_without_domain_or_markers_has_no_extra_lines():
    out = render_diff_summary(StagingChangeset(task_id="d2", creates=[_create()]))
    assert "домен:" not in out and "⚠" not in out and "теги: —" in out


def test_diff_update_shows_unverified_sections():
    upd = DraftNote(
        action=NoteAction.UPDATE, path="Знания/Старая.md", title="Старая",
        append_section="## Новый раздел\n\nТекст", unverified_sections=["Новый раздел"],
    )
    out = render_diff_summary(StagingChangeset(task_id="d3", updates=[upd]))
    assert "~ Знания/Старая.md" in out and "разделы требуют проверки: Новый раздел" in out


def test_diff_keeps_legacy_needs_review_marker():
    out = render_diff_summary(
        StagingChangeset(task_id="d4", creates=[_create(needs_review=True, critic_rounds=1)])
    )
    assert "критик не одобрил" in out

def test_diff_marks_moc():
    out = render_diff_summary(StagingChangeset(task_id="d5", creates=[_create(is_moc=True)]))
    assert "MOC" in out