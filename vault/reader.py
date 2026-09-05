"""
Прямой доступ к Vault через файловую систему (Вариант A, подтверждён).

Никогда не пишет в Vault — только читает. Запись — исключительно через
vault/writer.py и только на этапе Commit (после approve).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter as fm

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
INLINE_TAG_RE = re.compile(r"(?<!\S)#([\w\-/А-Яа-яЁё]+)")


@dataclass
class ParsedNote:
    path: str  # относительный путь внутри Vault, posix-стиль
    title: str
    frontmatter: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    body: str = ""
    raw_content: str = ""
    outlinks: list[str] = field(default_factory=list)  # заголовки, на которые ссылается
    content_hash: str = ""


def _compute_hash(raw_content: str) -> str:
    return hashlib.sha256(raw_content.encode("utf-8")).hexdigest()[:16]


def _extract_tags(frontmatter_data: dict, body: str) -> list[str]:
    tags: set[str] = set()
    fm_tags = frontmatter_data.get("tags")
    if isinstance(fm_tags, str):
        tags.add(fm_tags.lstrip("#"))
    elif isinstance(fm_tags, list):
        for t in fm_tags:
            tags.add(str(t).lstrip("#"))
    for m in INLINE_TAG_RE.finditer(body):
        tags.add(m.group(1))
    return sorted(tags)


def _extract_wikilinks(body: str) -> list[str]:
    return sorted({m.group(1).strip() for m in WIKILINK_RE.finditer(body)})


def _make_summary(body: str, max_chars: int = 400) -> str:
    """Простая детерминированная summary без LLM: первые содержательные строки."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    text = " ".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def parse_note_file(vault_path: Path, file_path: Path) -> ParsedNote:
    raw = file_path.read_text(encoding="utf-8")
    post = fm.loads(raw)
    rel_path = file_path.relative_to(vault_path).as_posix()
    title = str(post.metadata.get("title") or file_path.stem)
    tags = _extract_tags(post.metadata, post.content)
    outlinks = _extract_wikilinks(post.content)
    return ParsedNote(
        path=rel_path,
        title=title,
        frontmatter=dict(post.metadata),
        tags=tags,
        body=post.content,
        raw_content=raw,
        outlinks=outlinks,
        content_hash=_compute_hash(raw),
    )


def iter_markdown_files(vault_path: Path):
    for p in sorted(vault_path.rglob("*.md")):
        # пропускаем служебную папку .obsidian и наши рабочие директории, если внутри Vault
        if any(part.startswith(".") for part in p.relative_to(vault_path).parts[:-1]):
            continue
        yield p


def read_vault(vault_path: Path) -> list[ParsedNote]:
    if not vault_path.exists():
        raise FileNotFoundError(f"Vault path does not exist: {vault_path}")
    return [parse_note_file(vault_path, p) for p in iter_markdown_files(vault_path)]


def read_single_note(vault_path: Path, rel_path: str) -> ParsedNote | None:
    full = vault_path / rel_path
    if not full.exists():
        return None
    return parse_note_file(vault_path, full)


__all__ = [
    "ParsedNote",
    "parse_note_file",
    "iter_markdown_files",
    "read_vault",
    "read_single_note",
    "_make_summary",
]
