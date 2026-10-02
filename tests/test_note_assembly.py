from __future__ import annotations

from storage.models import OutlineNote, OutlineSubpoint, SectionDraft, NoteAnnotation, NoteAction
from tools.note_assembly import (
    PLACEHOLDER_MARKDOWN, add_domain_tag, assemble_note_markdown, close_unbalanced_fence,
    has_balanced_fences, normalize_headings, prepare_section, strip_leading_duplicate_heading, build_draft_note,
)


def _note(n: int = 2) -> OutlineNote:
    return OutlineNote(title="N", subpoints=[
        OutlineSubpoint(heading=h, covers="c") for h in "ABCDEFGHIJ"[:n]
    ])


def test_normalize_headings_clamps_levels_outside_fences():
    text = "# a\n### b\n##### c\n```python\n# comment\n##### not heading\n```\n"
    result = normalize_headings(text).splitlines()
    assert result[0] == "## a"
    assert result[1] == "### b"
    assert result[2] == "#### c"
    assert "# comment" in result
    assert "##### not heading" in result  # внутри кода не тронуто


def test_fence_balance_and_close():
    assert has_balanced_fences("```python\nx\n```")
    assert not has_balanced_fences("```python\nx")
    assert has_balanced_fences(close_unbalanced_fence("```python\nx"))


def test_strip_leading_duplicate_heading():
    assert strip_leading_duplicate_heading("## Определение\n\nТекст", "определение") == "Текст"
    assert strip_leading_duplicate_heading("Текст\n## Определение", "Определение").startswith("Текст")


def test_prepare_section_returns_fence_flag():
    text, ok = prepare_section("# Заг\n```python\nx", heading="Другой")
    assert text.startswith("## Заг")
    assert ok is False


def test_assemble_orders_sections_and_marks_unverified():
    note = _note(2)
    sec = SectionDraft(note_id=note.note_id, subpoint_id=note.subpoints[0].subpoint_id,
                       markdown="Текст A", needs_check=True)
    body, unverified = assemble_note_markdown(note, [sec], domain="technical")
    assert body.startswith("## A")
    assert body.index("## A") < body.index("## B")
    assert "[!warning] Требует проверки" in body
    assert PLACEHOLDER_MARKDOWN in body          # у B секции нет
    assert unverified == ["A", "B"]


def test_assemble_ignores_sections_of_other_notes():
    note = _note(1)
    foreign = SectionDraft(note_id="чужая", subpoint_id=note.subpoints[0].subpoint_id, markdown="Чужое")
    body, _ = assemble_note_markdown(note, [foreign], domain="technical")
    assert "Чужое" not in body


def test_abstract_only_from_threshold_and_not_for_update():
    big, small = _note(8), _note(7)
    assert "[!abstract]" in assemble_note_markdown(big, [], domain="technical", abstract="Резюме")[0]
    assert "[!abstract]" not in assemble_note_markdown(small, [], domain="technical", abstract="Резюме")[0]
    assert "[!abstract]" not in assemble_note_markdown(
        big, [], domain="technical", abstract="Резюме", for_update=True)[0]


def test_humanities_gets_general_warning_on_top():
    body, _ = assemble_note_markdown(_note(1), [], domain="humanities")
    assert body.startswith("> [!warning] Проверьте факты")


def test_add_domain_tag_no_duplicates():
    assert add_domain_tag(["rag"], "technical") == ["rag", "domain/technical"]
    assert add_domain_tag(["domain/technical"], "technical") == ["domain/technical"]

def _secs(note, **flags):
    return [
        SectionDraft(note_id=note.note_id, subpoint_id=sp.subpoint_id,
                     markdown=f"Текст {sp.heading}", needs_check=flags.get(sp.heading, False))
        for sp in note.subpoints
    ]


def test_build_draft_note_create_collects_tags_links_and_unverified():
    note = _note(2)
    note.folder = "Знания/Тема"
    ann = NoteAnnotation(note_id=note.note_id, tags=["rag"], links_out=["Другая"])
    draft = build_draft_note(note, _secs(note, A=True), ann, domain="technical",
                             mark_source="model-knowledge")

    assert draft.action == NoteAction.CREATE and draft.note_id == note.note_id
    assert draft.path == "Знания/Тема/N.md"
    assert draft.tags == ["rag", "domain/technical"]
    assert draft.links_out == ["Другая"]
    assert draft.unverified_sections == ["A"]
    assert draft.frontmatter["source"] == "model-knowledge"
    assert draft.body_md.startswith("## A") and draft.append_section is None


def test_build_draft_note_without_annotation_still_gets_domain_tag():
    note = _note(1)
    draft = build_draft_note(note, _secs(note), None, domain="life_management")
    assert draft.tags == ["domain/life_management"] and draft.links_out == []


def test_build_draft_note_abstract_only_for_big_create_notes():
    big = _note(8)
    ann = NoteAnnotation(note_id=big.note_id, abstract="Резюме")
    assert "[!abstract]" in build_draft_note(big, _secs(big), ann, domain="technical").body_md

    big.action, big.existing_path = "update", "Знания/Старая.md"
    upd = build_draft_note(big, _secs(big), ann, domain="technical")
    assert "[!abstract]" not in (upd.append_section or "")


def test_build_draft_note_update_uses_append_section_and_ignores_tags():
    note = _note(2)
    note.action, note.existing_path = "update", "Знания/Старая.md"
    ann = NoteAnnotation(note_id=note.note_id, tags=["x"], links_out=["Y"])
    draft = build_draft_note(note, _secs(note), ann, domain="technical")

    assert draft.action == NoteAction.UPDATE and draft.path == "Знания/Старая.md"
    assert draft.append_section.startswith("## A")
    assert draft.body_md == "" and draft.tags == [] and draft.links_out == []


def test_build_draft_note_update_without_subpoints_gives_none_for_validator():
    note = OutlineNote(title="Пусто", action="update", existing_path="Старая.md")
    assert build_draft_note(note, [], None, domain="technical").append_section is None


def test_build_draft_note_update_without_existing_path_raises():
    import pytest
    note = _note(1)
    note.action = "update"
    with pytest.raises(ValueError):
        build_draft_note(note, [], None, domain="technical")