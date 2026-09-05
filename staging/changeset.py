from __future__ import annotations

import json
from pathlib import Path

from storage.models import StagingChangeset
from tools.markdown_tools import render_markdown


def staging_task_dir(staging_dir: Path, task_id: str) -> Path:
    return staging_dir / task_id


def save_changeset(staging_dir: Path, changeset: StagingChangeset) -> Path:
    """
    Сохраняет changeset.json (машиночитаемый) и превью .md файлов
    (человекочитаемое, для ручного просмотра пользователем прямо в файловой
    системе, вне Vault).
    """
    task_dir = staging_task_dir(staging_dir, changeset.task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    (task_dir / "changeset.json").write_text(
        changeset.model_dump_json(indent=2), encoding="utf-8"
    )

    notes_dir = task_dir / "notes_preview"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for draft in changeset.creates + changeset.updates:
        preview_name = draft.path.replace("/", "__")
        content = draft.append_section or render_markdown(draft)
        (notes_dir / preview_name).write_text(content, encoding="utf-8")

    return task_dir


def load_changeset(staging_dir: Path, task_id: str) -> StagingChangeset | None:
    path = staging_task_dir(staging_dir, task_id) / "changeset.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return StagingChangeset.model_validate(data)


def list_pending_tasks(staging_dir: Path) -> list[str]:
    if not staging_dir.exists():
        return []
    return [p.name for p in staging_dir.iterdir() if p.is_dir() and (p / "changeset.json").exists()]
