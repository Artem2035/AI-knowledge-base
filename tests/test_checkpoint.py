from __future__ import annotations

import json

from staging.checkpoint import (
    CHECKPOINT_VERSION,
    TaskCheckpoint,
    delete_checkpoint,
    list_resumable_tasks,
    load_checkpoint,
    save_checkpoint,
)
from storage.models import (
    NoteAnnotation, OutlineNote, OutlineSubpoint, Plan, SectionDraft, TaskStatus,
)


def _checkpoint(task_id: str = "cp1") -> TaskCheckpoint:
    note = OutlineNote(
        title="N", subpoints=[OutlineSubpoint(heading="H", covers="c", kind="context")]
    )
    sp = note.subpoints[0]
    plan = Plan(task_id=task_id, topic_title="Тема", domain="humanities", notes=[note])
    return TaskCheckpoint(
        task_id=task_id, raw_query="q", language="ru",
        status=TaskStatus(task_id=task_id),
        plan=plan, plan_approved=True,
        elaborated_subpoint_ids=[sp.subpoint_id],
        sections=[SectionDraft(
            note_id=note.note_id, subpoint_id=sp.subpoint_id, markdown="Текст", needs_check=True,
        )],
        annotated_note_ids=[note.note_id],
        annotations=[NoteAnnotation(note_id=note.note_id, tags=["т"], abstract="Резюме")],
    )


def test_roundtrip_keeps_sections_annotations_and_plan_metadata(tmp_path):
    cp = _checkpoint()
    save_checkpoint(tmp_path, cp)
    loaded = load_checkpoint(tmp_path, "cp1")

    assert loaded is not None and loaded.version == CHECKPOINT_VERSION == 5
    assert loaded.sections[0].needs_check is True
    assert loaded.sections[0].markdown == "Текст"
    assert loaded.annotations[0].abstract == "Резюме"
    assert loaded.plan.domain == "humanities"
    assert loaded.plan.notes[0].subpoints[0].kind == "context"
    assert loaded.elaborated_subpoint_ids == cp.elaborated_subpoint_ids


def test_new_checkpoint_has_clean_defaults_and_no_legacy_fields():
    cp = TaskCheckpoint(task_id="x", raw_query="q", language="ru", status=TaskStatus(task_id="x"))
    assert not cp.elaboration_done and not cp.vault_analysis_done and not cp.annotation_done
    assert cp.sections == [] and cp.annotations == []
    for legacy in ("evidence", "drafts", "written_note_indices", "synthesis_done",
                   "extraction_done", "extracted_unit_ids", "existing_notes", "relationships"):
        assert legacy not in TaskCheckpoint.model_fields


def test_v4_checkpoint_is_not_loaded(tmp_path):
    save_checkpoint(tmp_path, _checkpoint("old"))
    path = tmp_path / "old.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_checkpoint(tmp_path, "old") is None


def test_list_resumable_skips_v4_and_returns_v5(tmp_path):
    save_checkpoint(tmp_path, _checkpoint("old"))
    save_checkpoint(tmp_path, _checkpoint("new"))
    path = tmp_path / "old.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert [c.task_id for c in list_resumable_tasks(tmp_path)] == ["new"]


def test_corrupted_json_returns_none(tmp_path):
    (tmp_path / "bad.json").write_text("{не json", encoding="utf-8")
    assert load_checkpoint(tmp_path, "bad") is None


def test_save_is_atomic_no_tmp_left_and_delete_works(tmp_path):
    save_checkpoint(tmp_path, _checkpoint())
    assert not list(tmp_path.glob("*.tmp"))
    delete_checkpoint(tmp_path, "cp1")
    assert load_checkpoint(tmp_path, "cp1") is None