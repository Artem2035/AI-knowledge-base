from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from storage.models import StagingChangeset
from vault.db import VaultDB
from vault.index import VaultIndexer
from vault.writer import VaultWriter, WriteResult

logger = logging.getLogger(__name__)


class CommitError(Exception):
    pass


def commit_changeset(
    changeset: StagingChangeset,
    vault_path: Path,
    db: VaultDB,
    embedder,
    allow_delete: bool,
    git_enabled: bool,
    backup_dir: Path,
) -> list[WriteResult]:
    """
    Единственная функция во всей системе, которая переносит staged-изменения
    в настоящий Vault. Вызывается ТОЛЬКО после явного approve пользователя
    в CLI. Ничего здесь не делает "автоматически" в фоне.
    """
    if changeset.validation is None or not changeset.validation.ok:
        raise CommitError(
            "Попытка commit changeset, не прошедшего валидацию (или без "
            "validation вообще). Это должно быть невозможно при нормальном "
            "workflow через Orchestrator — остановка как защитная мера."
        )

    if changeset.deletes and not allow_delete:
        raise CommitError("Changeset содержит deletes, но ALLOW_DELETE=false.")

    writer = VaultWriter(vault_path=vault_path, allow_delete=allow_delete)
    results: list[WriteResult] = []

    for draft in changeset.creates:
        writer.ensure_folder(draft.folder or "")
        results.append(writer.write_draft(draft))

    for draft in changeset.updates:
        results.append(writer.write_draft(draft, backup_dir=backup_dir))

    for path in changeset.deletes:  # в MVP по умолчанию всегда пустой список
        writer.delete_note(path)

    # переиндексация только затронутых файлов — дёшево, инкрементально
    indexer = VaultIndexer(db=db, vault_path=vault_path, embedder=embedder)
    indexer.sync()
    indexer.resolve_wikilink_targets()

    if git_enabled:
        _git_commit(vault_path, changeset)

    return results


def _git_commit(vault_path: Path, changeset: StagingChangeset) -> None:
    try:
        subprocess.run(["git", "-C", str(vault_path), "add", "-A"], check=True, capture_output=True)
        message = (
            f"obsidian-ai-kb: задача {changeset.task_id} "
            f"(+{len(changeset.creates)} новых, ~{len(changeset.updates)} дополнено)"
        )
        subprocess.run(
            ["git", "-C", str(vault_path), "commit", "-m", message],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # Git — вспомогательная функция, не должна ломать основной workflow,
        # если, например, нечего коммитить или Vault ещё не git-репозиторий.
        logger.warning("Git commit не выполнен: %s", exc.stderr.decode(errors="ignore") if exc.stderr else exc)
