"""
Интерактивное отображение и редактирование Plan (outline) в консоли —
вызывается из cli/main.py::_confirm_plan как plan_confirm_cb, сразу после
outline_planner.build_plan(), ДО самого дорогого по Gemini/Groq-бюджету
этапа elaboration (см. orchestrator/state_machine.py::run).

Мутирует переданный Plan напрямую (add/remove/rename на plan.notes и
note.subpoints) — Orchestrator сохраняет уже изменённый объект в
checkpoint.plan при persist("plan_approved"), отдельной синхронизации не
требуется.
"""
from __future__ import annotations

import typer
import re
from rich.console import Console
from rich.tree import Tree

from storage.models import OutlineNote, OutlineSubpoint, Plan, DOMAIN_KINDS, normalize_kind

_KIND_LABELS_RU = {
    "definition": "определение", "mechanism": "механизм", "parameters": "параметры",
    "example": "пример кода/формула", "comparison": "сравнение", "pitfalls": "подводные камни",
    "context": "контекст", "key_idea": "ключевая идея", "interpretations": "трактовки и аргументы",
    "terms_persons": "термины и персоналии", "critique": "критика", "connections": "связи",
    "principle": "принцип", "when_to_apply": "когда применять", "steps": "шаги/фреймворк",
    "scenario": "пример-сценарий", "mistakes": "типичные ошибки", "checklist": "чек-лист",
    "other": "другое",
}
_DOMAIN_LABELS_RU = {
    "technical": "техническая (математика, программирование, техника)",
    "humanities": "гуманитарная",
    "life_management": "life management (навыки, привычки, продуктивность)",
}

_INDICES_RE = re.compile(r"\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*")
_INDEX_PART_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?")

console = Console()


def build_plan_tree(plan: Plan) -> Tree:
    # Корень дерева — summary темы
    root = Tree(f"{plan.summary or plan.topic_title}  · домен: {plan.domain}")
    for i, note in enumerate(plan.notes, start=1):
        note_branch = root.add(f"{i}. {note.title}")
        for sp in note.subpoints:
            note_branch.add(f"{sp.heading} ({sp.kind}): {sp.covers}")
    return root

def _prompt_kind(domain: str, default: str = "other") -> str:
    """Выбор kind из набора домена (+ 'other'). Enter — оставить default."""
    kinds = list(DOMAIN_KINDS.get(domain, ())) + ["other"]
    for i, k in enumerate(kinds, start=1):
        console.print(f"    {i}. {k} — {_KIND_LABELS_RU[k]}")
    default_idx = kinds.index(default) + 1 if default in kinds else len(kinds)
    idx = typer.prompt(f"  Тип раздела (1-{len(kinds)})", type=int, default=default_idx)
    if not (1 <= idx <= len(kinds)):
        console.print("[red]Неверный номер — оставлен прежний тип.[/red]")
        return default if default in kinds else "other"
    return kinds[idx - 1]

def _add_note(plan: Plan) -> None:
    title = typer.prompt("Заголовок новой заметки").strip()
    if not title:
        console.print("[red]Пустой заголовок — отменено.[/red]")
        return
    note = OutlineNote(title=title)
    while typer.confirm("Добавить подпункт к этой заметке?", default=True):
        heading = typer.prompt("  Заголовок подпункта").strip()
        covers = typer.prompt("  Техзадание (covers)").strip()
        if heading:
            kind = _prompt_kind(plan.domain)
            note.subpoints.append(OutlineSubpoint(heading=heading, covers=covers, kind=kind))
    plan.notes.append(note)


def _remove_note(plan: Plan) -> None:
    if not plan.notes:
        console.print("[dim]Заметок нет.[/dim]")
        return
    raw = typer.prompt(f"Номера заметок для удаления через запятую (1-{len(plan.notes)})")
    indices = _parse_indices(raw, len(plan.notes))
    if indices is None:
        return

    removed_titles = [plan.notes[i].title for i in indices]  # уже в порядке убывания индексов
    for i in indices:
        plan.notes.pop(i)

    if len(removed_titles) == 1:
        console.print(f"[dim]Удалена заметка «{removed_titles[0]}».[/dim]")
    else:
        listing = ", ".join(f"«{t}»" for t in reversed(removed_titles))  # вернуть исходный порядок для вывода
        console.print(f"[dim]Удалено заметок: {len(removed_titles)} ({listing}).[/dim]")


def _rename_note(plan: Plan) -> None:
    idx = _select_note_index(plan, "Номер заметки для переименования")
    if idx is None:
        return
    new_title = typer.prompt("Новый заголовок", default=plan.notes[idx].title).strip()
    if new_title:
        plan.notes[idx].title = new_title


def _add_subpoint(plan: Plan) -> None:
    idx = _select_note_index(plan, "К какой заметке добавить подпункт")
    if idx is None:
        return
    heading = typer.prompt("Заголовок подпункта").strip()
    covers = typer.prompt("Техзадание (covers)").strip()
    if heading:
        kind = _prompt_kind(plan.domain)
        plan.notes[idx].subpoints.append(OutlineSubpoint(heading=heading, covers=covers, kind=kind))


def _remove_subpoint(plan: Plan) -> None:
    idx = _select_note_index(plan, "Из какой заметки удалить подпункт(ы)")
    if idx is None:
        return
    note = plan.notes[idx]
    if not note.subpoints:
        console.print("[dim]Подпунктов нет.[/dim]")
        return

    for i, sp in enumerate(note.subpoints, start=1):
        console.print(f"  {i}. {sp.heading}")
    raw = typer.prompt(f"Номера подпунктов через запятую (1-{len(note.subpoints)})")
    indices = _parse_indices(raw, len(note.subpoints))
    if indices is None:
        return

    removed_headings = [note.subpoints[i].heading for i in indices]
    for i in indices:
        note.subpoints.pop(i)

    if len(removed_headings) == 1:
        console.print(f"[dim]Удалён подпункт «{removed_headings[0]}».[/dim]")
    else:
        listing = ", ".join(f"«{h}»" for h in reversed(removed_headings))
        console.print(f"[dim]Удалено подпунктов: {len(removed_headings)} ({listing}).[/dim]")


def _edit_subpoint(plan: Plan) -> None:
    idx = _select_note_index(plan, "В какой заметке изменить подпункт")
    if idx is None:
        return
    note = plan.notes[idx]
    sp_idx = _select_subpoint_index(note)
    if sp_idx is None:
        return
    sp = note.subpoints[sp_idx]
    sp.heading = typer.prompt("Заголовок", default=sp.heading).strip() or sp.heading
    sp.covers = typer.prompt("Техзадание (covers)", default=sp.covers).strip() or sp.covers
    sp.kind = _prompt_kind(plan.domain, sp.kind)

def _change_domain(plan: Plan) -> None:
    """Смена домена. kind, не входящие в набор нового домена, сбрасываются в 'other'."""
    domains = list(DOMAIN_KINDS)
    for i, d in enumerate(domains, start=1):
        console.print(f"  {i}. {d} — {_DOMAIN_LABELS_RU[d]}")
    default_idx = domains.index(plan.domain) + 1 if plan.domain in domains else 1
    idx = typer.prompt(f"Домен (1-{len(domains)})", type=int, default=default_idx)
    if not (1 <= idx <= len(domains)):
        console.print("[red]Неверный номер.[/red]")
        return
    new_domain = domains[idx - 1]
    if new_domain == plan.domain:
        return
    reset = 0
    for note in plan.notes:
        for sp in note.subpoints:
            new_kind = normalize_kind(sp.kind, new_domain)
            if new_kind != sp.kind:
                sp.kind, reset = new_kind, reset + 1
    plan.domain = new_domain
    console.print(f"[dim]Домен: {new_domain}. Сброшено в 'other' типов разделов: {reset}.[/dim]")

def _select_note_index(plan: Plan, prompt: str) -> int | None:
    if not plan.notes:
        console.print("[dim]Заметок нет.[/dim]")
        return None
    idx = typer.prompt(f"{prompt} (1-{len(plan.notes)})", type=int)
    if not (1 <= idx <= len(plan.notes)):
        console.print("[red]Неверный номер.[/red]")
        return None
    return idx - 1

def _parse_indices(raw: str, count: int) -> list[int] | None:
    """Разбирает строку вида '2,4,5' или '1,3-5' (1-based номера и
    диапазоны) в список 0-based индексов по УБЫВАНИЮ без дублей — так
    элементы можно удалять последовательными pop() без пересчёта."""
    if not _INDICES_RE.fullmatch(raw):
        console.print(
            "[red]Не удалось разобрать номера — используйте запятую как "
            "разделитель и дефис для диапазонов (например: 1,3-5).[/red]"
        )
        return None
    indices: set[int] = set()
    for m in _INDEX_PART_RE.finditer(raw):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start > end:
            start, end = end, start
        # границы проверяем ДО раскрытия диапазона
        if start < 1 or end > count:
            console.print(f"[red]Номер вне диапазона [1, {count}].[/red]")
            return None
        indices.update(range(start - 1, end))
    return sorted(indices, reverse=True)

def _select_subpoint_index(note: OutlineNote) -> int | None:
    if not note.subpoints:
        console.print("[dim]Подпунктов нет.[/dim]")
        return None
    for i, sp in enumerate(note.subpoints, start=1):
        console.print(f"  {i}. {sp.heading}")
    idx = typer.prompt(f"Номер подпункта (1-{len(note.subpoints)})", type=int)
    if not (1 <= idx <= len(note.subpoints)):
        console.print("[red]Неверный номер.[/red]")
        return None
    return idx - 1

_EDIT_MENU = (
    ("1", "Утвердить план"),
    ("2", "Отменить задачу"),
    ("3", "Добавить заметку"),
    ("4", "Удалить заметку(и)"),
    ("5", "Переименовать заметку"),
    ("6", "Добавить подпункт"),
    ("7", "Удалить подпункт(ы)"),
    ("8", "Изменить подпункт (заголовок/covers/тип)"),
    ("9", "Изменить домен"),
)
_EDIT_ACTIONS = {
    "3": _add_note, "4": _remove_note, "5": _rename_note,
    "6": _add_subpoint, "7": _remove_subpoint, "8": _edit_subpoint,
    "9": _change_domain,
}


def confirm_plan(plan: Plan) -> bool:
    """Показывает план, даёт отредактировать (add/remove/rename на
    заметках и подпунктах) и утвердить/отменить. Используется как
    plan_confirm_cb в Orchestrator.run() (см. orchestrator/state_machine.py)."""
    while True:
        console.print(plan.topic_title)
        console.print(build_plan_tree(plan))
        total_subpoints = sum(len(n.subpoints) for n in plan.notes)
        console.print(f"\nЗаметок: {len(plan.notes)}, подпунктов всего: {total_subpoints}\n")

        for key, label in _EDIT_MENU:
            console.print(f"  {key}. {label}")
        action = typer.prompt("Действие", default="1").strip()

        if action == "1":
            if not plan.notes:
                console.print("[red]План пуст — добавьте хотя бы одну заметку.[/red]\n")
                continue
            return True
        if action == "2":
            return False
        handler = _EDIT_ACTIONS.get(action)
        if handler is None:
            console.print("[red]Неизвестное действие.[/red]")
            continue
        handler(plan)
        console.print()