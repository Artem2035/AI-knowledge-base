"""
Контракты structured-output для каждого LLM-вызова.

Намеренно отделены от storage/models.py: LLM не должна сама придумывать
task_id/path/source_id (это foreign keys, которыми управляет код) — вместо
этого модель ссылается на индексы элементов, переданных ей в промпте, а
код-обвязка роли уже сама подставляет реальные id/пути. Это снижает риск
галлюцинаций в структурных полях.

Простые типы (str/float/bool/list) вместо произвольных dict — потому что
JSON Schema, которую LLM использует для structured output, работает
надёжнее с фиксированной формой полей.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from storage.models import Kind, Domain


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class OutlineNoteOutput(BaseModel):
    title: str
    subpoints: list[OutlineSubpointOutput] = Field(default_factory=list)
    rationale: str = ""


class OutlineSubpointOutput(BaseModel):
    heading: str
    covers: str
    kind: Kind = "other"


class OutlinePlanOutput(BaseModel):
    topic_title: str
    domain: Domain = "technical"
    summary: str = ""
    notes: list[OutlineNoteOutput] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# Researcher (отбор источников) — используется только в RESEARCH_MODE=web
# ---------------------------------------------------------------------------


class SourceSelectionItem(BaseModel):
    index: int  # индекс в списке кандидатов, переданном в промпте (0-based)
    relevance_score: float = Field(ge=0.0, le=1.0)
    keep: bool


class SourceSelectionOutput(BaseModel):
    items: list[SourceSelectionItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Extractor + Critic (объединены) — используется только в RESEARCH_MODE=web
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    concept: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_definition: bool = False
    # Индекс "единицы" (чанка ОДНОГО источника) в списке, переданном в
    # промпте текущего батча — позволяет батчить чанки НЕСКОЛЬКИХ разных
    # источников в одном вызове и корректно приписать каждое утверждение
    # его настоящему source_id в коде-обвязке (см. roles/extractor_critic.py
    # :: _to_evidence_list), а не доверять LLM формулировать source_id самой.
    unit_index: int = 0
    contradicts_indices: list[int] = Field(default_factory=list)
    critic_note: str = ""


class EvidenceBatchOutput(BaseModel):
    evidence: list[EvidenceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Vault dedup (только "серая зона")
# ---------------------------------------------------------------------------


class DedupDecisionOutput(BaseModel):
    same_concept: bool
    decision: Literal["reuse", "extend", "distinct"]
    reasoning: str = ""

# ---------------------------------------------------------------------------
# папки для заметок
# ---------------------------------------------------------------------------
class FolderAssignmentItem(BaseModel):
    index: int
    folder: str

class FolderAssignmentOutput(BaseModel):
    items: list[FolderAssignmentItem] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# Elaborator v2 — готовый markdown секции (RESEARCH_MODE=knowledge)
# ---------------------------------------------------------------------------

class SectionItem(BaseModel):
    # Обязательное поле (без default): пропущенный индекс не должен молча
    # превращаться в 0 и приписывать текст чужому разделу.
    unit_index: int
    markdown: str
    needs_check: bool = False  # модель не уверена в деталях раздела


class SectionBatchOutput(BaseModel):
    sections: list[SectionItem] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# Annotator: теги, ссылки и резюме заметки (RESEARCH_MODE=knowledge, v2)
# ---------------------------------------------------------------------------
class AnnotationItem(BaseModel):
    # Обязательное поле (без default): пропущенный индекс не должен молча
    # превращаться в 0 и приписывать аннотацию чужой заметке.
    index: int
    tags: list[str] = Field(default_factory=list)
    links_out: list[str] = Field(default_factory=list)
    abstract: str = ""


class AnnotationBatchOutput(BaseModel):
    items: list[AnnotationItem] = Field(default_factory=list)