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
from tools.markdown_tools import build_note_path, normalize_link_title
from tools.note_assembly import HUMANITIES_CALLOUT, close_unbalanced_fence

_FENCE_LINE_RE = re.compile(r"^\s{0,3}```")
_HEADING_LINE_RE = re.compile(r"^(#{1,6})(\s+.*)$")


def _shift_headings(text: str, levels: int = 1) -> str:
    """Увеличивает уровень каждого Markdown-заголовка на `levels`
    (## -> ###, потолок ######), НЕ трогая строки внутри fenced code
    blocks. Разбор построчный: незакрытый fence считается кодом до конца
    текста, поэтому '# comment' в нём никогда не станет заголовком."""
    if not text:
        return text

    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEADING_LINE_RE.match(line)
            if m:
                line = "#" * min(len(m.group(1)) + levels, 6) + m.group(2)
        out.append(line)
    return "\n".join(out)


def _strip_humanities_callout(text: str) -> tuple[str, bool]:
    """Вырезает общий humanities-callout из начала тела заметки (его
    ставит tools/note_assembly.py). Возвращает (текст, был_ли_callout)."""
    if text.startswith(HUMANITIES_CALLOUT):
        return text[len(HUMANITIES_CALLOUT):].lstrip("\n"), True
    return text, False


def merge_drafts(
    drafts: list[DraftNote], indices: list[int], merged_title: str = "", merged_path: str = "",
) -> DraftNote:
    """
    Объединяет несколько готовых DraftNote (action=create) в один.

    Что делает (без LLM):
    - оборачивает body_md каждой исходной заметки в "## {title}" и сдвигает
      её внутренние заголовки на 1 уровень глубже (код не трогается);
    - закрывает висящий fence в исходном теле ДО склейки, иначе он
      поглотил бы следующие секции;
    - общий humanities-callout вырезает из исходных тел и ставит один раз
      сверху; резюме (abstract) каждой исходной заметки остаётся под её
      собственным "## {title}", общее резюме не создаётся;
    - объединяет tags/source_refs/unverified_sections с дедупликацией;
    - links_out: убирает ссылки на заметки, вошедшие в объединение, и
      самоссылки (после слияния они указывали бы на несуществующие
      заголовки); ссылки ДРУГИХ заметок на исходные заголовки
      перенаправляет fix_links_after_merge();
    - merged_from хранит заголовки исходных заметок.

    note_id объединённого драфта пуст: исходный план для объединённой
    структуры не актуален, validate_headings_coverage для него — no-op.

    Исключения (ValueError): меньше 2 уникальных индексов; индекс вне
    диапазона; среди выбранных есть action != create.
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

    title = merged_title.strip() or " + ".join(d.title for d in sources)
    folder = sources[0].folder
    path = merged_path.strip() or build_note_path(folder, title)

    internal_keys = {normalize_link_title(d.title) for d in sources} | {normalize_link_title(title)}

    body_parts: list[str] = []
    all_tags: list[str] = []
    all_links: list[str] = []
    all_sources: list[str] = []
    all_unverified: list[str] = []
    merged_from: list[str] = []
    any_needs_review, any_humanities, max_critic_rounds = False, False, 0

    for d in sources:
        body, had_callout = _strip_humanities_callout(d.body_md.strip())
        any_humanities = any_humanities or had_callout
        body = close_unbalanced_fence(body)
        body_parts.append(f"## {d.title}\n\n{_shift_headings(body)}")

        for t in d.tags:
            if t not in all_tags:
                all_tags.append(t)
        for link in d.links_out:
            if normalize_link_title(link) in internal_keys or link in all_links:
                continue
            all_links.append(link)
        for ref in d.source_refs:
            if ref not in all_sources:
                all_sources.append(ref)
        for heading in d.unverified_sections:
            if heading not in all_unverified:
                all_unverified.append(heading)
        merged_from.extend(d.merged_from or [d.title])
        any_needs_review = any_needs_review or d.needs_review
        max_critic_rounds = max(max_critic_rounds, d.critic_rounds)

    if any_humanities:
        body_parts.insert(0, HUMANITIES_CALLOUT)

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
        unverified_sections=all_unverified,
        merged_from=merged_from,
    )


def fix_links_after_merge(drafts: list[DraftNote]) -> list[DraftNote]:
    """Проход по links_out ПОСЛЕ apply_merges/merge_all_drafts: ссылки на
    заголовки, ушедшие в объединённую заметку, перенаправляются на её
    заголовок (по DraftNote.merged_from); самоссылки и дубли удаляются.
    Нужен отдельным шагом: заметка вне группы слияния может ссылаться на
    заголовок, который теперь живёт в чужой объединённой заметке.
    Если слияний не было (ни у кого нет merged_from) — возвращает drafts
    без изменений."""
    redirects = {
        normalize_link_title(old): d.title for d in drafts for old in d.merged_from
    }
    if not redirects:
        return drafts

    result: list[DraftNote] = []
    for d in drafts:
        own = normalize_link_title(d.title)
        new_links: list[str] = []
        for link in d.links_out:
            target = redirects.get(normalize_link_title(link), link)
            if normalize_link_title(target) == own or target in new_links:
                continue
            new_links.append(target)
        result.append(d.model_copy(update={"links_out": new_links}) if new_links != d.links_out else d)
    return result

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