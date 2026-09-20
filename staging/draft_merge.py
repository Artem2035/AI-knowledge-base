"""
Объединение уже НАПИСАННЫХ заметок (Writer+Critic пройдены) в одну —
финальный, опциональный шаг ПЕРЕД validation/staging (см.
config/settings.py::enable_draft_merging, orchestrator/state_machine.py).

Принципиально: здесь НЕТ ни одного вызова LLM. Каждая исходная заметка
уже содержит готовый body_md со своими "##"-заголовками (из подпунктов
плана) — merge_drafts просто:
  1) оборачивает body_md каждой исходной заметки в "## {исходный title}";
  2) сдвигает её внутренние заголовки на 1 уровень глубже, чтобы не
     столкнуться на одном уровне с заголовками других объединяемых
     заметок;
  3) объединяет tags/links_out/source_refs с дедупликацией.

links_out после объединения не требует отдельной обработки: это уже ОДИН
DraftNote, а markdown_tools.render_markdown и так кладёт весь links_out
ОДНОЙ секцией '## Связанные заметки' в конце файла — "куча ссылок внизу"
получается автоматически, тем же кодом, что и для обычной заметки.
"""
from __future__ import annotations

import re

from storage.models import DraftNote, NoteAction
from tools.markdown_tools import build_note_path

_HEADING_RE = re.compile(r'^(#{1,5})(\s)', re.MULTILINE)
_CODE_FENCE_RE = re.compile(r'```.*?```', re.DOTALL)


def _shift_headings(text: str, levels: int = 1) -> str:
    """Увеличивает уровень каждого Markdown-заголовка на `levels`
    (## -> ###), НЕ трогая '#'-подобные последовательности внутри
    fenced code blocks (```...```) — иначе строки вида '# comment'
    в примерах кода сломались бы."""
    if not text:
        return text

    def _bump(m: re.Match) -> str:
        return ("#" * min(len(m.group(1)) + levels, 6)) + m.group(2)

    parts, last_end = [], 0
    for m in _CODE_FENCE_RE.finditer(text):
        parts.append(_HEADING_RE.sub(_bump, text[last_end:m.start()]))
        parts.append(m.group(0))  # код не трогаем
        last_end = m.end()
    parts.append(_HEADING_RE.sub(_bump, text[last_end:]))
    return "".join(parts)


def merge_drafts(
    drafts: list[DraftNote], indices: list[int], merged_title: str = "", merged_path: str = "",
) -> DraftNote:
    """
    Объединяет несколько готовых DraftNote (action=create) в один.

    note_id объединённого драфта оставляется пустым: он использовался
    только для validate_headings_coverage по исходному Plan
    (validation/markdown_validator.py) — на этом этапе исходная
    заметка-план для объединённой структуры уже не актуальна, проверка
    просто не выполняется для этого драфта (note is None -> no-op в
    validate_headings_coverage), это сознательный компромисс.

    Объединение UPDATE-черновиков (append_section) не поддерживается —
    у них нет самостоятельного body_md для рендера как раздела.
    """
    sorted_idx = sorted(set(indices))
    if len(sorted_idx) < 2:
        raise ValueError("Для объединения нужно минимум 2 разных черновика")
    if any(i < 0 or i >= len(drafts) for i in sorted_idx):
        raise ValueError(f"Индекс вне диапазона [0, {len(drafts)})")

    sources = [drafts[i] for i in sorted_idx]
    non_create = [d.title for d in sources if d.action != NoteAction.CREATE]
    if non_create:
        raise ValueError(f"Объединение поддерживается только для action=create: {', '.join(non_create)}")

    body_parts, all_tags, all_links, all_sources = [], [], [], []
    any_needs_review, max_critic_rounds = False, 0

    for d in sources:
        body_parts.append(f"## {d.title}\n\n{_shift_headings(d.body_md.strip())}")
        for t in d.tags:
            if t not in all_tags:
                all_tags.append(t)
        for link in d.links_out:
            if link not in all_links:
                all_links.append(link)
        for ref in d.source_refs:
            if ref not in all_sources:
                all_sources.append(ref)
        any_needs_review = any_needs_review or d.needs_review
        max_critic_rounds = max(max_critic_rounds, d.critic_rounds)

    title = merged_title.strip() or " + ".join(d.title for d in sources)
    folder = sources[0].folder
    path = merged_path.strip() or build_note_path(folder, title)

    return DraftNote(
        note_id="",
        action=NoteAction.CREATE,
        path=path,
        title=title,
        folder=folder,
        frontmatter=dict(sources[0].frontmatter),
        body_md="\n\n".join(body_parts),
        tags=all_tags,
        links_out=all_links,
        source_refs=all_sources,
        critic_rounds=max_critic_rounds,
        needs_review=any_needs_review,
    )


def apply_merges(drafts: list[DraftNote], merge_groups: list[tuple[list[int], str]]) -> list[DraftNote]:
    """Применяет НЕСКОЛЬКО непересекающихся групп слияния за один проход.
    merge_groups — [(индексы, заголовок объединённой заметки), ...].
    Объединённый драфт встаёт на место первого индекса своей группы;
    заметки вне групп сохраняют исходный порядок."""
    all_indices = [i for indices, _ in merge_groups for i in indices]
    if len(all_indices) != len(set(all_indices)):
        raise ValueError("Группы объединения не должны пересекаться")

    merged_by_first: dict[int, DraftNote] = {}
    consumed: set[int] = set()
    for indices, title in merge_groups:
        merged_by_first[min(indices)] = merge_drafts(drafts, indices, merged_title=title)
        consumed.update(indices)

    return [
        merged_by_first[i] if i in merged_by_first else d
        for i, d in enumerate(drafts) if i not in consumed or i in merged_by_first
    ]

def merge_all_drafts(drafts: list[DraftNote], merged_title: str = "") -> list[DraftNote]:
    """
    draft_merge_mode="all" (см. config/settings.py) — объединяет ВСЕ
    action=create черновики в один, без выбора пользователя.

    action=update черновики (дополнения СУЩЕСТВУЮЩИХ заметок Vault) в
    объединение НЕ включаются ни при каком режиме — merge_drafts
    принципиально работает только с action=create (см. её докстринг): у
    update-черновика нет самостоятельного body_md для рендера как раздела,
    только append_section к уже существующему файлу Vault.

    Если create-черновиков меньше двух — сливать нечего, возвращает
    drafts без изменений (не ошибка: например, задача создала одну новую
    заметку и дополнила несколько существующих).
    """
    create_indices = [i for i, d in enumerate(drafts) if d.action == NoteAction.CREATE]
    if len(create_indices) < 2:
        return drafts
    return apply_merges(drafts, [(create_indices, merged_title)])