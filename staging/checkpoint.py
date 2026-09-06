"""
Чекпоинты задач для resume после остановки по бюджету Gemini.

Формат: один JSON-файл на задачу, <checkpoint_dir>/<task_id>.json,
перезаписывается ПОСЛЕ КАЖДОГО завершённого шага (а не только в конце
пайплайна). Это принципиально отличается от StagingChangeset — тот
создаётся только на последнем шаге ("staged"), поэтому бесполезен, если
задача остановилась раньше (а именно так и происходит при исчерпании
MAX_GEMINI_CALLS_PER_TASK на этапе extracting — самом "дорогом" по числу
вызовов).

Инвариант: чекпоинт хранит уже провалидированные структурированные данные
(Plan, SourceCandidate[], Evidence[], ...) — те же Pydantic-модели, что и
так летают между roles/*, поэтому сериализация/десериализация тривиальна
и не завязана на конкретный LLM-провайдер.

Гранулярность resume:
- Planning / Researching-selection / Fetching / Vault-analysis / Synthesis —
  шаг целиком "сделан" или "не сделан" (флаг *_done).
- Extracting — самый частый источник остановки, поэтому гранулярность
  на уровне ОТДЕЛЬНОГО ИСТОЧНИКА: extracted_source_ids хранит source_id
  уже обработанных источников, чтобы при resume не пересчитывать evidence
  по источникам, которые уже были обработаны до остановки.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from gemini.schemas import NotePlanOutput
from storage.models import (
    DraftNote,
    Evidence,
    ExistingNote,
    Plan,
    Relationship,
    SourceCandidate,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# Бампать при несовместимых изменениях структуры чекпоинта — старые
# чекпоинты с другой версией просто не загрузятся (см. load_checkpoint),
# вместо того чтобы упасть с невнятной ошибкой валидации Pydantic.
CHECKPOINT_VERSION = 1


class TaskCheckpoint(BaseModel):
    """Снимок состояния задачи после последнего успешно завершённого шага."""

    version: int = CHECKPOINT_VERSION
    task_id: str
    raw_query: str
    language: str

    # Человекочитаемая метка последнего завершённого шага — используется
    # только для отображения пользователю (resumable/сообщения), логика
    # resume опирается на *_done флаги ниже, а не на эту строку.
    last_completed_stage: str = "created"

    status: TaskStatus

    # Накопительный расход Gemini-вызовов по ВСЕМ попыткам (для отчёта
    # пользователю). Отдельно от status.gemini_calls_used, который считает
    # вызовы только в рамках ТЕКУЩЕЙ сессии/попытки — см.
    # MAX_GEMINI_CALLS_PER_TASK в orchestrator/budget.py: лимит применяется
    # к сессии, иначе задачу нельзя было бы никогда докрутить после
    # однократного исчерпания.
    total_gemini_calls_used: int = 0

    plan: Plan | None = None

    raw_candidates_done: bool = False
    raw_candidates: list[SourceCandidate] = Field(default_factory=list)

    sources_selected_done: bool = False
    selected_sources: list[SourceCandidate] = Field(default_factory=list)

    sources_fetched_done: bool = False
    fetched_sources: list[SourceCandidate] = Field(default_factory=list)

    extraction_done: bool = False
    extracted_source_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    vault_analysis_done: bool = False
    existing_notes: list[ExistingNote] = Field(default_factory=list)

    synthesis_done: bool = False
    # Map-reduce: шаг 1 (план заметок) и шаг 2 (запись по одной заметке)
    # персистятся отдельно — резюм не должен пересчитывать ни план, ни уже
    # написанные заметки (аналогично extracted_source_ids для extraction).
    note_plan_done: bool = False
    note_plan: NotePlanOutput | None = None
    written_note_indices: list[int] = Field(default_factory=list)
    drafts: list[DraftNote] = Field(default_factory=list)  # накапливается по одной заметке
    relationships: list[Relationship] = Field(default_factory=list)

    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def checkpoint_path(checkpoint_dir: Path, task_id: str) -> Path:
    return Path(checkpoint_dir) / f"{task_id}.json"


def save_checkpoint(checkpoint_dir: Path, checkpoint: TaskCheckpoint) -> None:
    """Атомарная запись: пишем во временный файл и переименовываем поверх
    основного — так kill -9/сбой посреди записи не оставит битый JSON
    поверх рабочего чекпоинта. Актуально, т.к. пишем ЧАСТО (после каждого
    источника на этапе extracting)."""
    checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
    path = checkpoint_path(checkpoint_dir, checkpoint.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)


def load_checkpoint(checkpoint_dir: Path, task_id: str) -> TaskCheckpoint | None:
    path = checkpoint_path(checkpoint_dir, task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Чекпоинт %s повреждён (невалидный JSON): %s", path, exc)
        return None

    if data.get("version") != CHECKPOINT_VERSION:
        logger.warning(
            "Чекпоинт %s имеет версию %s, ожидалась %s — resume недоступен, "
            "запустите задачу заново ('ask').",
            path, data.get("version"), CHECKPOINT_VERSION,
        )
        return None

    try:
        return TaskCheckpoint.model_validate(data)
    except Exception as exc:  # ValidationError и т.п.
        logger.error("Чекпоинт %s не прошёл валидацию: %s", path, exc)
        return None


def delete_checkpoint(checkpoint_dir: Path, task_id: str) -> None:
    """Вызывается после успешного доведения задачи до staging — чекпоинт
    больше не нужен, дальнейшее состояние живёт в StagingChangeset."""
    checkpoint_path(checkpoint_dir, task_id).unlink(missing_ok=True)


def list_resumable_tasks(checkpoint_dir: Path) -> list[TaskCheckpoint]:
    """Все задачи, у которых есть незавершённый чекпоинт (ещё не staged)."""
    dir_path = Path(checkpoint_dir)
    if not dir_path.exists():
        return []
    result: list[TaskCheckpoint] = []
    for f in sorted(dir_path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("version") == CHECKPOINT_VERSION:
                result.append(TaskCheckpoint.model_validate(data))
        except Exception as exc:
            logger.warning("Пропускаем повреждённый чекпоинт %s: %s", f, exc)
    return result