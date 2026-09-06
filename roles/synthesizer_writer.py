from __future__ import annotations

from datetime import datetime, timezone

from llm.base import LLMClient
from gemini.schemas import SynthesisOutput
from storage.models import (
    DraftNote,
    Evidence,
    ExistingNote,
    NoteAction,
    Plan,
    Relationship,
    SourceCandidate,
    TaskStatus,
)
from tools.markdown_tools import build_note_path, normalize_link_title, sanitize_wikilinks

SYSTEM_INSTRUCTION = (
    "Ты одновременно выполняешь роли Synthesizer и Obsidian Writer. "
    "На основе плана исследования, извлечённых фактов и списка уже "
    "существующих заметок пользователя реши: какие заметки нужно СОЗДАТЬ "
    "заново (action=create), а какие уже покрыты существующей заметкой и "
    "её нужно только ДОПОЛНИТЬ (action=update, с append_section — новым "
    "материалом для добавления, БЕЗ переписывания уже существующего "
    "содержимого). Главное правило: если для концепции есть существующая "
    "заметка с decision='reuse' или 'extend' — используй action=update и "
    "existing_path=путь этой заметки, не создавай дубликат. Заметки должны "
    "быть атомарными (одна концепция = одна заметка), с частыми "
    "[[wikilink]]-ссылками на другие заметки, которые ты создаёшь в этом же "
    "ответе, и на существующие заметки, если это уместно. Пиши на русском "
    "языке (кроме общепринятых технических терминов вроде SQL, Python, RAG "
    "— их не переводить). Каждая заметка должна ссылаться на источники в "
    "своём содержании (упоминать откуда факт), но НЕ копировать текст "
    "источника дословно — только пересказ своими словами."
    "Для action=create обязательно верни body_md с полным содержимым новой заметки."
    "Для action=update обязательно верни existing_path и append_section с новым материалом."
    "Для action=update поле body_md можно не возвращать: существующее содержимое заметки необходимо сохранить без переписывания."
    "Правила по ссылкам: используй заголовок существующей заметки ТОЧНО "
    "так, как он дан в списке существующих заметок (включая дефисы). "
    "Ссылку на ЕЩЁ НЕ созданную заметку ([[Новая тема]]) добавляй только "
    "для действительно самостоятельной темы, которая может стать отдельной "
    "группой заметок — не для каждого незнакомого термина и не для "
    "терминов, являющихся составной частью текущей темы. "
    "Правила по объёму: каждая заметка с action=create должна содержать "
    "минимум 3 содержательных абзаца (не считая заголовков/фронтматтера); "
    "если материал по смежным подтемам умещается в 1 абзац каждый — "
    "объедини его в ОДНУ заметку со структурой из подзаголовков, а не "
    "создавай несколько коротких заметок. Где уместно, добавляй примеры "
    "кода с указанием языка (```python, ```cmd, ```json и т.д.)."
)


def synthesize_and_write(
    plan: Plan,
    evidence: list[Evidence],
    sources: list[SourceCandidate],
    existing_notes: list[ExistingNote],
    client: LLMClient,
    status: TaskStatus,
    default_folder: str,
) -> tuple[list[DraftNote], list[Relationship]]:
    evidence_listing = "\n".join(
        f"- [{e.concept}] {e.statement} (confidence={e.confidence:.2f}"
        f"{', ПРОТИВОРЕЧИВО: ' + e.critic_note if e.critic_note else ''})"
        for e in evidence
    )
    existing_listing = "\n".join(
        f"- path={n.path!r} title={n.title!r} decision={n.decision} "
        f"(похоже на концепцию {n.matched_concept!r}, similarity={n.similarity_score:.2f})"
        for n in existing_notes
    ) or "(похожих существующих заметок не найдено)"
    sources_listing = "\n".join(f"- {s.title}: {s.url}" for s in sources)

    prompt = (
        f"Тема: {plan.topic_title}\n"
        f"Описание: {plan.summary}\n"
        f"Подтемы: {', '.join(s.title for s in plan.subtopics)}\n\n"
        f"Извлечённые факты/тезисы:\n{evidence_listing}\n\n"
        f"Существующие заметки в Vault, похожие на новые концепции:\n{existing_listing}\n\n"
        f"Источники (для frontmatter/ссылок, url):\n{sources_listing}\n\n"
        "Сформируй итоговый набор заметок (create/update)."
    )

    output: SynthesisOutput = client.generate_structured(
        role="synthesizer_writer",
        prompt=prompt,
        response_model=SynthesisOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    # Карта "нормализованный заголовок" -> "точное написание в Vault",
    # чтобы поймать случаи вроде разных Unicode-дефисов в
    # заметках и снапнуть ссылку на реальное имя файла, а не флагать её
    # как битую.
    existing_by_normalized = {
        normalize_link_title(n.title): n.title for n in existing_notes
    }

    now_iso = datetime.now(timezone.utc).date().isoformat()
    drafts: list[DraftNote] = []
    for note_out in output.notes:
        frontmatter = {"created": now_iso}
        for f in note_out.frontmatter_extra:
            frontmatter[f.key] = f.value

        if note_out.action == "update": #and note_out.existing_path:
            #path = note_out.existing_path
            #action = NoteAction.UPDATE
            if not note_out.existing_path:
                raise ValueError(
                    f"Для action='update' не указан existing_path: {note_out.title!r}"
                )

            path = note_out.existing_path
            action = NoteAction.UPDATE
        else:
            path = build_note_path(note_out.folder or default_folder, note_out.title)
            action = NoteAction.CREATE

        links_out_fixed = []
        for raw_link in note_out.links_out:
            link = sanitize_wikilinks(raw_link).strip()
            canonical = existing_by_normalized.get(normalize_link_title(link))
            links_out_fixed.append(canonical or link)

        drafts.append(
            DraftNote(
                action=action,
                path=path,
                title=note_out.title,
                folder=note_out.folder or default_folder,
                frontmatter=frontmatter,
                body_md=sanitize_wikilinks(note_out.body_md),
                tags=note_out.tags,
                links_out=links_out_fixed,
                source_refs=[s.url for s in sources],
                append_section=sanitize_wikilinks(note_out.append_section) or None,
            )
        )

    draft_titles_normalized = {normalize_link_title(d.title): d.title for d in drafts}
    for d in drafts:
        d.links_out = [
            draft_titles_normalized.get(normalize_link_title(t), existing_by_normalized.get(normalize_link_title(t), t))
            for t in d.links_out
        ]

    relationships = _build_relationships(drafts)
    return drafts, relationships


def _build_relationships(drafts: list[DraftNote]) -> list[Relationship]:
    relationships: list[Relationship] = []
    title_to_path = {d.title: d.path for d in drafts}
    for d in drafts:
        for linked_title in d.links_out:
            target_path = title_to_path.get(linked_title, linked_title)
            relationships.append(
                Relationship(from_note=d.path, to_note=target_path, link_type="wikilink")
            )
    return relationships
