from __future__ import annotations

import pytest

from staging.draft_merge import apply_merges, merge_drafts, fix_links_after_merge
from storage.models import DraftNote, NoteAction
from staging.draft_merge import merge_all_drafts
from tools.note_assembly import has_balanced_fences, HUMANITIES_CALLOUT


def _draft(title, body, tags=None, links=None, refs=None, needs_review=False, critic_rounds=0) -> DraftNote:
    return DraftNote(
        action=NoteAction.CREATE, path=f"Знания/{title}.md", title=title,
        body_md=body, tags=tags or [], links_out=links or [], source_refs=refs or [],
        needs_review=needs_review, critic_rounds=critic_rounds,
    )


def test_merge_wraps_each_source_as_h2_and_shifts_inner_headings():
    d1 = _draft("Embeddings", "## Определение\n\nТекст.")
    d2 = _draft("Vector DB", "## Определение\n\nДругой текст.")
    merged = merge_drafts([d1, d2], [0, 1], merged_title="Retrieval basics")

    assert merged.title == "Retrieval basics"
    assert "## Embeddings" in merged.body_md
    assert "## Vector DB" in merged.body_md
    assert "### Определение" in merged.body_md  # сдвинуто на уровень глубже
    assert merged.body_md.count("### Определение") == 2


def test_merge_does_not_shift_headings_inside_code_fences():
    d1 = _draft("A", "## Раздел\n\n```python\n# comment, not a heading\n```")
    d2 = _draft("B", "## Раздел\n\nТекст.")
    merged = merge_drafts([d1, d2], [0, 1])
    assert "# comment, not a heading" in merged.body_md  # не тронуто


def test_merge_combines_tags_links_sources_with_dedup():
    d1 = _draft("A", "Текст.", tags=["rag"], links=["X"], refs=["https://a"])
    d2 = _draft("B", "Текст.", tags=["rag", "ml"], links=["X", "Y"], refs=["https://a", "https://b"])
    merged = merge_drafts([d1, d2], [0, 1])
    assert merged.tags == ["rag", "ml"]
    assert merged.links_out == ["X", "Y"]
    assert merged.source_refs == ["https://a", "https://b"]


def test_merge_propagates_needs_review_and_max_critic_rounds():
    d1 = _draft("A", "Текст.", needs_review=False, critic_rounds=0)
    d2 = _draft("B", "Текст.", needs_review=True, critic_rounds=2)
    merged = merge_drafts([d1, d2], [0, 1])
    assert merged.needs_review is True
    assert merged.critic_rounds == 2


def test_merge_rejects_update_drafts():
    d1 = _draft("A", "Текст.")
    d2 = DraftNote(action=NoteAction.UPDATE, path="X.md", title="B", append_section="доп.")
    with pytest.raises(ValueError):
        merge_drafts([d1, d2], [0, 1])


def test_merge_note_id_is_empty_skips_headings_coverage_safely():
    d1 = _draft("A", "Текст.")
    d2 = _draft("B", "Текст.")
    merged = merge_drafts([d1, d2], [0, 1])
    assert merged.note_id == ""


def test_apply_merges_multiple_non_overlapping_groups():
    drafts = [_draft(t, "Текст.") for t in ["A", "B", "C", "D"]]
    result = apply_merges(drafts, [([0, 1], "AB"), ([2, 3], "CD")])
    assert [d.title for d in result] == ["AB", "CD"]


def test_apply_merges_rejects_overlapping_groups():
    drafts = [_draft(t, "Текст.") for t in ["A", "B", "C"]]
    with pytest.raises(ValueError):
        apply_merges(drafts, [([0, 1], "X"), ([1, 2], "Y")])


def test_apply_merges_no_groups_returns_drafts_unchanged():
    drafts = [_draft("A", "Текст."), _draft("B", "Текст.")]
    assert apply_merges(drafts, []) == drafts

def test_merge_all_drafts_merges_every_create_draft():
    drafts = [_draft(t, "Текст.") for t in ["A", "B", "C"]]
    result = merge_all_drafts(drafts, merged_title="Итог")
    assert len(result) == 1
    assert result[0].title == "Итог"
    assert "## A" in result[0].body_md
    assert "## B" in result[0].body_md
    assert "## C" in result[0].body_md


def test_merge_all_drafts_skips_update_drafts():
    creates = [_draft("A", "Текст."), _draft("B", "Текст.")]
    update = DraftNote(action=NoteAction.UPDATE, path="X.md", title="X", append_section="доп.")
    result = merge_all_drafts([creates[0], update, creates[1]], merged_title="Итог")

    assert len(result) == 2  # объединённая + update, не тронутый
    assert any(d.action == NoteAction.UPDATE and d.title == "X" for d in result)
    merged = next(d for d in result if d.title == "Итог")
    assert "## A" in merged.body_md and "## B" in merged.body_md


def test_merge_all_drafts_noop_with_fewer_than_two_creates():
    drafts = [_draft("A", "Текст.")]
    assert merge_all_drafts(drafts, merged_title="X") == drafts


def test_merge_all_drafts_noop_when_zero_creates():
    update = DraftNote(action=NoteAction.UPDATE, path="X.md", title="X", append_section="доп.")
    assert merge_all_drafts([update]) == [update]

def test_merge_carries_unverified_sections_with_dedup():
    d1 = _draft("A", "Текст.").model_copy(update={"unverified_sections": ["Р1", "Общий"]})
    d2 = _draft("B", "Текст.").model_copy(update={"unverified_sections": ["Общий", "Р2"]})
    merged = merge_drafts([d1, d2], [0, 1])
    assert merged.unverified_sections == ["Р1", "Общий", "Р2"]


def test_merge_keeps_humanities_callout_once_on_top():
    d1 = _draft("A", HUMANITIES_CALLOUT + "\n\n## Р\n\nТекст A.")
    d2 = _draft("B", HUMANITIES_CALLOUT + "\n\n## Р\n\nТекст B.")
    merged = merge_drafts([d1, d2], [0, 1])
    assert merged.body_md.startswith(HUMANITIES_CALLOUT)
    assert merged.body_md.count("Проверьте факты") == 1


def test_merge_shift_respects_heading_ceiling():
    d1 = _draft("A", "## a\n### b\n#### c\n##### d\n###### e")
    merged = merge_drafts([d1, _draft("B", "Текст.")], [0, 1])
    lines = merged.body_md.splitlines()
    assert "### a" in lines and "#### b" in lines and "##### c" in lines
    assert "###### d" in lines and "###### e" in lines  # потолок ######


def test_merge_closes_unclosed_fence_and_does_not_shift_code():
    d1 = _draft("A", "## Р\n\n```python\n# c")
    d2 = _draft("B", "## Р2\n\nТекст.")
    merged = merge_drafts([d1, d2], [0, 1])
    assert has_balanced_fences(merged.body_md)
    assert "\n# c\n" in merged.body_md  # строка кода не стала заголовком
    assert "\n## B\n" in merged.body_md  # следующая секция вне блока кода


def test_merge_drops_internal_and_self_links_and_records_sources():
    d1 = _draft("A", "Текст.", links=["B", "X"])
    d2 = _draft("B", "Текст.", links=["A", "Y", "AB"])
    merged = merge_drafts([d1, d2], [0, 1], merged_title="AB")
    assert merged.links_out == ["X", "Y"]
    assert merged.merged_from == ["A", "B"]


def test_fix_links_redirects_external_links_to_merged_note():
    drafts = [
        _draft("A", "Текст."), _draft("B", "Текст."),
        _draft("C", "Текст.", links=["A", "B", "D"]),
        _draft("D", "Текст.", links=["B", "C"]),
    ]
    result = fix_links_after_merge(apply_merges(drafts, [([0, 1], "AB")]))
    by_title = {d.title: d for d in result}
    assert by_title["C"].links_out == ["AB", "D"]   # A и B схлопнулись в одну ссылку
    assert by_title["D"].links_out == ["AB", "C"]


def test_fix_links_noop_without_merges():
    drafts = [_draft("A", "Текст.", links=["B"]), _draft("B", "Текст.")]
    assert fix_links_after_merge(drafts) == drafts