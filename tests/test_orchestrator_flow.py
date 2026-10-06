from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from config.settings import Settings
from llm.schemas import (
    AnnotationBatchOutput, AnnotationItem, FolderAssignmentItem, FolderAssignmentOutput,
    OutlineNoteOutput, OutlinePlanOutput, OutlineSubpointOutput, SectionBatchOutput, SectionItem,
)
from orchestrator.budget import LLMTaskBudgetExceeded
from orchestrator.state_machine import Orchestrator
from staging.checkpoint import load_checkpoint

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "test_vault"

# Названия намеренно не пересекаются со словами заметок test_vault, чтобы
# BM25-дедупликация гарантированно дала action="create".
_TITLE_A, _TITLE_B = "Alpha gizmo", "Beta gadget"

_SECTION_TEXT = (
    "Первый содержательный абзац раздела, достаточно длинный для проверки.\n\n"
    "Второй содержательный абзац раздела, тоже достаточно длинный для проверки.\n\n"
    "Третий содержательный абзац раздела, и он тоже достаточно длинный."
)


class _MainFake:
    """Мок основного клиента (self.llm): planner, folder_assignment, annotator."""

    def __init__(self):
        self.calls: list[str] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.calls.append(role)
        if role == "outline_planner":
            return OutlinePlanOutput(
                topic_title="Quokka topic", domain="technical",
                notes=[
                    OutlineNoteOutput(title=_TITLE_A, subpoints=[
                        OutlineSubpointOutput(heading="Intro one", covers="c", kind="definition"),
                        OutlineSubpointOutput(heading="Details two", covers="c", kind="mechanism"),
                    ]),
                    OutlineNoteOutput(title=_TITLE_B, subpoints=[
                        OutlineSubpointOutput(heading="Usage three", covers="c", kind="example"),
                    ]),
                ],
            )
        if role == "folder_assignment":
            # Пустая папка не входит в допустимые -> код подставит default_folder.
            return FolderAssignmentOutput(items=[FolderAssignmentItem(index=i, folder="") for i in range(2)])
        if role == "annotator":
            n = prompt.count("=== Заметка [")
            items = [AnnotationItem(index=i, tags=["Тест Тег"]) for i in range(n)]
            items[0].links_out = [_TITLE_B]
            return AnnotationBatchOutput(items=items)
        raise AssertionError(f"неожиданная роль: {role!r}")


class _ExtractionFake:
    """Мок extraction-клиента: elaborator; fail_on_call — номер вызова,
    на котором поднимается LLMTaskBudgetExceeded (эмуляция лимита)."""

    def __init__(self, fail_on_call: int | None = None):
        self.fail_on_call = fail_on_call
        self.calls: list[str] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        assert role == "elaborator"
        self.calls.append(role)
        if self.fail_on_call == len(self.calls):
            raise LLMTaskBudgetExceeded("тестовый лимит вызовов")
        n = prompt.count("=== Раздел [")
        return SectionBatchOutput(sections=[
            SectionItem(unit_index=i, markdown=_SECTION_TEXT) for i in range(n)
        ])


def _settings(tmp_path: Path, **overrides) -> Settings:
    vault = tmp_path / "vault"
    if not vault.exists():
        shutil.copytree(FIXTURE_VAULT, vault)
    base = dict(
        llm_provider="groq", groq_api_key="fake", free_only=True,
        vault_path=vault, workdir=tmp_path / "work",
        staging_dir=tmp_path / "work" / "staging",
        db_path=tmp_path / "work" / "index.sqlite3",
        checkpoint_dir=tmp_path / "work" / "checkpoints",
        use_local_embeddings=False, enable_draft_merging=False,
        max_subpoints_per_generation_batch=3,
    )
    base.update(overrides)
    return Settings(**base)


def _orchestrator(monkeypatch, settings, main, extraction) -> Orchestrator:
    monkeypatch.setattr("orchestrator.state_machine.create_llm_client", lambda s, b: main)
    monkeypatch.setattr(
        "orchestrator.state_machine.create_extraction_llm_client",
        lambda s, b, primary_client=None: extraction,
    )
    return Orchestrator(settings)


def test_full_flow_produces_valid_changeset(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    main, ext = _MainFake(), _ExtractionFake()
    orch = _orchestrator(monkeypatch, settings, main, ext)
    try:
        result = orch.run(raw_query="Изучи тему")
    finally:
        orch.close()

    assert not result.stopped
    assert result.changeset.validation.ok, [i.message for i in result.changeset.validation.errors]
    assert len(result.changeset.creates) == 3 and not result.changeset.updates
    moc = [d for d in result.changeset.creates if d.is_moc]
    assert len(moc) == 1 and moc[0].title == "Quokka topic — обзор"
    assert moc[0].links_out == [_TITLE_A, _TITLE_B]
    assert "source" not in moc[0].frontmatter

    draft_a = next(d for d in result.changeset.creates if d.title == _TITLE_A)
    assert draft_a.path == f"Знания/Quokka topic/{_TITLE_A}.md"
    assert draft_a.tags == ["тест-тег", "domain/technical"]
    assert draft_a.links_out == [_TITLE_B]
    assert draft_a.frontmatter["source"] == "model-knowledge"
    assert "## Intro one" in draft_a.body_md and "## Details two" in draft_a.body_md

    assert ext.calls == ["elaborator"]  # 3 подпункта, батч по 3
    assert main.calls.count("outline_planner") == 1
    assert main.calls.count("annotator") == 1
    assert "critic" not in main.calls and "synthesizer_write" not in main.calls
    assert result.changeset.relationships  # A -> B

    # задача дошла до staging: чекпоинт удалён, changeset на диске
    assert load_checkpoint(settings.checkpoint_dir, result.task_id) is None
    assert (settings.staging_dir / result.task_id / "changeset.json").exists()


def test_resume_after_budget_stop_skips_finished_sections(tmp_path, monkeypatch):
    settings = _settings(tmp_path, max_subpoints_per_generation_batch=1)  # 3 батча по 1

    main1, ext1 = _MainFake(), _ExtractionFake(fail_on_call=2)
    orch = _orchestrator(monkeypatch, settings, main1, ext1)
    try:
        first = orch.run(raw_query="Изучи тему")
    finally:
        orch.close()

    assert first.stopped and first.changeset is None
    cp = load_checkpoint(settings.checkpoint_dir, first.task_id)
    assert cp is not None
    assert len(cp.elaborated_subpoint_ids) == 1 and len(cp.sections) == 1
    assert not cp.elaboration_done and cp.plan_approved

    main2, ext2 = _MainFake(), _ExtractionFake()
    orch = _orchestrator(monkeypatch, settings, main2, ext2)
    try:
        second = orch.run(resume_task_id=first.task_id)
    finally:
        orch.close()

    assert not second.stopped and second.changeset.validation.ok
    assert ext2.calls == ["elaborator", "elaborator"]  # только 2 оставшихся подпункта
    assert "outline_planner" not in main2.calls  # план взят из чекпоинта
    assert load_checkpoint(settings.checkpoint_dir, first.task_id) is None


def test_plan_rejected_stops_before_elaboration(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    main, ext = _MainFake(), _ExtractionFake()
    orch = _orchestrator(monkeypatch, settings, main, ext)
    try:
        result = orch.run(raw_query="Изучи тему", plan_confirm_cb=lambda plan: False)
    finally:
        orch.close()

    assert result.stopped and ext.calls == []
    cp = load_checkpoint(settings.checkpoint_dir, result.task_id)
    assert cp is not None and cp.plan is not None and not cp.plan_approved


def test_merge_callback_runs_before_relationships(tmp_path, monkeypatch):
    from staging.draft_merge import merge_all_drafts

    settings = _settings(tmp_path, enable_draft_merging=True)
    orch = _orchestrator(monkeypatch, settings, _MainFake(), _ExtractionFake())
    try:
        result = orch.run(
            raw_query="Изучи тему",
            merge_confirm_cb=lambda drafts: merge_all_drafts(drafts, merged_title="Объединённая"),
        )
    finally:
        orch.close()

    assert [d.title for d in result.changeset.creates] == ["Объединённая"]
    # после слияния ссылка A -> B стала самоссылкой на уже несуществующую
    # заметку; relationships строятся по ИТОГОВЫМ черновикам
    paths = {d.path for d in result.changeset.creates}
    assert all(r.from_note in paths for r in result.changeset.relationships)
    assert not any(d.is_moc for d in result.changeset.creates)