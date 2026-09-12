from __future__ import annotations

from llm.base import LLMClient
from gemini.prompts.vault_analyst import SYSTEM_INSTRUCTION
from gemini.schemas import DedupDecisionOutput
from retrieval.search import RetrievalHit, VaultSearcher
from storage.models import Plan, TaskStatus
from tools.dedup import classify_similarity


def resolve_notes_against_vault(
    plan: Plan,
    searcher: VaultSearcher,
    client: LLMClient,
    status: TaskStatus,
    existing_folders: list[str],
    default_folder: str,
    high_threshold: float,
    low_threshold: float,
) -> None:
    """Мутирует plan.notes[].action/existing_path/folder НА МЕСТЕ.
    LLM вызывается только для кандидатов в "серой зоне" схожести — как и
    раньше, детерминированные пороги решают явные случаи кодом."""
    for note in plan.notes:
        query = note.title + " " + " ".join(sp.heading for sp in note.subpoints)
        hits: list[RetrievalHit] = searcher.search(query, top_k=1)

        if not hits:
            note.action, note.folder = "create", _pick_folder(note, existing_folders, default_folder)
            continue

        score = hits[0].combined_score
        verdict = classify_similarity(score, high_threshold, low_threshold)

        if verdict == "distinct":
            note.action, note.folder = "create", _pick_folder(note, existing_folders, default_folder)
        elif verdict == "duplicate":
            note.action, note.existing_path = "update", hits[0].path
        else:  # ambiguous -> 1 маленький вызов
            decision = _resolve_ambiguous(note, hits[0], client, status)
            if decision in ("reuse", "extend"):
                note.action, note.existing_path = "update", hits[0].path
            else:
                note.action, note.folder = "create", _pick_folder(note, existing_folders, default_folder)


def _pick_folder(note, existing_folders: list[str], default_folder: str) -> str:
    """Детерминированная эвристика: совпадение слов заголовка заметки с
    именами существующих папок Vault, иначе — папка по умолчанию для
    задачи. Осознанное упрощение по сравнению с прежним LLM-выбором папки —
    качество эвристики видно в staging-diff до approve."""
    title_words = set(note.title.lower().split())
    for folder in existing_folders:
        folder_name = folder.rsplit("/", 1)[-1].lower()
        if folder_name in title_words or any(w in folder_name for w in title_words):
            return folder
    return default_folder


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