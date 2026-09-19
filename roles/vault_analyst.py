from __future__ import annotations

from llm.base import LLMClient
from llm.prompts.vault_analyst import SYSTEM_INSTRUCTION, FOLDER_SYSTEM_INSTRUCTION
from llm.schemas import DedupDecisionOutput, FolderAssignmentOutput
from llm.chunking import split_items_into_batches
from retrieval.search import RetrievalHit, VaultSearcher
from storage.models import Plan, TaskStatus
from tools.dedup import classify_similarity


def resolve_notes_against_vault(
    plan: Plan, searcher: VaultSearcher, client: LLMClient, status: TaskStatus,
    existing_folders: list[str], default_folder: str,
    high_threshold: float, low_threshold: float,
) -> None:
    notes_needing_folder: list = []

    for note in plan.notes:
        query = note.title + " " + " ".join(sp.heading for sp in note.subpoints)
        hits = searcher.search(query, top_k=1)

        if not hits:
            note.action = "create"
            notes_needing_folder.append(note)
            continue

        score = hits[0].combined_score
        verdict = classify_similarity(score, high_threshold, low_threshold)

        if verdict == "distinct":
            note.action = "create"
            notes_needing_folder.append(note)
        elif verdict == "duplicate":
            note.action, note.existing_path = "update", hits[0].path
        else:
            decision = _resolve_ambiguous(note, hits[0], client, status)
            if decision in ("reuse", "extend"):
                note.action, note.existing_path = "update", hits[0].path
            else:
                note.action = "create"
                notes_needing_folder.append(note)

    if notes_needing_folder:
        _assign_folders_batch(notes_needing_folder, existing_folders, default_folder, client, status)


def _assign_folders_batch(notes, existing_folders, default_folder, client, status) -> None:
    """Один (иногда несколько, при большом числе новых заметок) пакетный
    вызов на ВСЕ заметки разом — не один вызов на заметку."""
    folders_listing = "\n".join(f"- {f}" for f in existing_folders) or "(папок пока нет)"
    static_overhead = (
        f"Существующие папки Vault:\n{folders_listing}\n\n"
        f"Папка по умолчанию для этой темы: {default_folder}\n\n"
        "Для каждой заметки укажи index и folder (см. правила роли)."
    )

    def _render(n) -> str:
        return f"{n.title}\nРазделы: {', '.join(sp.heading for sp in n.subpoints)}"

    batches = split_items_into_batches(
        notes, client=client, system_instruction=FOLDER_SYSTEM_INSTRUCTION,
        response_model=FolderAssignmentOutput, render_item=_render,
        static_overhead_text=static_overhead,
    )

    valid_folders = set(existing_folders) | {default_folder}
    for batch in batches:
        listing = "\n".join(
            f"[{i}] {n.title}\nРазделы: {', '.join(sp.heading for sp in n.subpoints)}"
            for i, n in enumerate(batch)
        )
        prompt = f"{static_overhead}\n\nЗаметки:\n\n{listing}"
        output: FolderAssignmentOutput = client.generate_structured(
            role="folder_assignment", prompt=prompt, response_model=FolderAssignmentOutput,
            status=status, system_instruction=FOLDER_SYSTEM_INSTRUCTION,
        )
        assigned = set()
        for item in output.items:
            if 0 <= item.index < len(batch):
                folder = item.folder.strip().strip("/")
                batch[item.index].folder = folder if folder in valid_folders else default_folder
                assigned.add(item.index)

        # safety net: если модель пропустила какую-то заметку в ответе —
        # не оставляем folder пустым, используем папку по умолчанию.
        for i, n in enumerate(batch):
            if i not in assigned:
                n.folder = default_folder


def _resolve_ambiguous(note, hit: RetrievalHit, client: LLMClient, status: TaskStatus) -> str:
    prompt = (
        f"Новая заметка из плана: {note.title!r}\n"
        f"Разделы: {', '.join(sp.heading for sp in note.subpoints)}\n\n"
        f"Существующая заметка:\n"
        f"Заголовок: {hit.title}\n"
        f"Теги: {', '.join(hit.tags)}\n"
        f"Краткое содержание: {hit.summary}\n\n"
        "Это та же концепция?"
    )
    output: DedupDecisionOutput = client.generate_structured(
        role="vault_dedup", prompt=prompt, response_model=DedupDecisionOutput,
        status=status, system_instruction=SYSTEM_INSTRUCTION,
    )
    return output.decision