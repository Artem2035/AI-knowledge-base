from __future__ import annotations

import logging
from dataclasses import dataclass

from gemini.schemas import EvidenceBatchOutput
from llm.base import LLMClient
from llm.chunking import split_items_into_batches
from llm.groq_client import GroqPromptTooLargeError, GroqSchemaError
from storage.models import Evidence, Plan, SourceCandidate, TaskStatus

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Ты одновременно выполняешь две роли: Extractor и Critic/Fact-Checker. "
    "Extractor: извлеки из текста ключевые факты, определения, тезисы и "
    "аргументы, относящиеся к теме исследования. Каждое утверждение должно "
    "быть атомарным (одна мысль) и привязано к одной из заданных "
    "концепций/подтем. Тебе может быть дан текст СРАЗУ ИЗ НЕСКОЛЬКИХ "
    "пронумерованных единиц (возможно, из разных источников) — для КАЖДОГО "
    "утверждения обязательно укажи unit_index, из какой именно единицы оно "
    "извлечено. Critic: для каждого утверждения оцени confidence (0-1) по "
    "качеству источника и ясности формулировки; если утверждение выглядит "
    "непроверенным, спорным или противоречит здравому смыслу — снизь "
    "confidence и опиши это в critic_note. Если находишь противоречие между "
    "утверждениями из РАЗНЫХ единиц (в т.ч. разных источников) — используй "
    "contradicts_indices. Не включай маркетинговые утверждения и воду без "
    "фактического содержания."
)

# Целевой размер ОДНОЙ "единицы" — намеренно заметно МЕНЬШЕ типичного
# TPM-бюджета одного вызова, чтобы несколько единиц из РАЗНЫХ источников
# можно было упаковать в один вызов (см. extract_evidence_from_sources).
# Это статическая константа, а НЕ запрос текущего доступного бюджета у
# клиента (как было в v2) — иначе разбиение источника на единицы менялось
# бы между сессиями (в зависимости от недавней истории вызовов клиента),
# и unit_id (source_id#chunk_index) переставал бы совпадать при resume.
_UNIT_TARGET_CHARS = 3000
# Минимальный размер единицы, ниже которого дальше делить нет смысла —
# если модель не справляется даже с таким куском, пропускаем его с
# предупреждением, а не уходим в бесконечную рекурсию.
_MIN_UNIT_CHARS = 1200
# Сколько раз пробуем бисекцию батча при GroqSchemaError, прежде чем сдаться.
_MAX_SPLIT_DEPTH = 2


@dataclass
class ExtractionUnit:
    source: SourceCandidate
    chunk_text: str
    chunk_index: int  # порядковый номер чанка ВНУТРИ этого источника

    @property
    def unit_id(self) -> str:
        return f"{self.source.source_id}#{self.chunk_index}"


def _split_text(text: str, chunk_chars: int) -> list[str]:
    """Режет текст на чанки примерно по chunk_chars символов, стараясь не
    рвать предложения посередине."""
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            search_from = max(end - int(chunk_chars * 0.2), start)
            boundary = max(
                text.rfind("\n", search_from, end),
                text.rfind(". ", search_from, end),
            )
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


def build_extraction_units(
    sources: list[SourceCandidate],
    max_chars: int = 8000,
    max_units_per_source: int = 3,
) -> list[ExtractionUnit]:
    """Чистый код, без LLM: строит units для всех источников. Детерминировано
    (статический _UNIT_TARGET_CHARS) — можно безопасно пересчитывать заново
    на каждом resume, unit_id не "поплывёт"."""
    units: list[ExtractionUnit] = []
    for source in sources:
        if not source.fetched_text:
            continue
        text = source.fetched_text[:max_chars]
        chunks = _split_text(text, _UNIT_TARGET_CHARS)
        if len(chunks) > max_units_per_source:
            logger.warning(
                "Источник %s разбивается на %d единиц, что превышает потолок "
                "max_units_per_source=%d — обрабатываем только первые %d "
                "(~%d%% текста).",
                source.url, len(chunks), max_units_per_source,
                max_units_per_source, int(100 * max_units_per_source / len(chunks)),
            )
            chunks = chunks[:max_units_per_source]
        for idx, chunk_text in enumerate(chunks):
            units.append(ExtractionUnit(source=source, chunk_text=chunk_text, chunk_index=idx))
    return units


def extract_evidence_from_sources(
    sources: list[SourceCandidate],
    plan: Plan,
    client: LLMClient,
    status: TaskStatus,
    already_done_unit_ids: set[str],
    on_batch_done,
    max_chars: int = 8000,
    max_units_per_source: int = 3,
) -> None:
    """Извлекает evidence сразу из НЕСКОЛЬКИХ источников, упаковывая их
    единицы в батчи под доступный TPM-бюджет клиента (см. llm/chunking.py) —
    в отличие от прежней версии, где каждый источник обрабатывался
    изолированно и "недогруженный хвост" каждого источника (последний,
    обычно неполный чанк) тратил впустую место в TPM-окне.

    on_batch_done(unit_ids: list[str], new_evidence: list[Evidence]) — колбэк,
    вызываемый ПОСЛЕ КАЖДОГО батча; вызывающий код (orchestrator) обязан
    сохранить unit_ids в чекпоинт немедленно, иначе resume потеряет прогресс.
    """
    concepts = ", ".join(s.title for s in plan.subtopics)
    all_units = build_extraction_units(
        sources, max_chars=max_chars, max_units_per_source=max_units_per_source
    )
    remaining_units = [u for u in all_units if u.unit_id not in already_done_unit_ids]
    if not remaining_units:
        return

    static_overhead = (
        f"Тема исследования: {plan.topic_title}\n"
        f"Известные подтемы/концепции: {concepts}\n\n"
        "Извлеки факты/определения/тезисы из ВСЕХ единиц, привязывая каждый "
        "к наиболее подходящей концепции (или сформулируй свою). Для "
        "каждого факта укажи unit_index — номер единицы, из которой он "
        "извлечён."
    )

    batches = split_items_into_batches(
        remaining_units,
        client=client,
        system_instruction=SYSTEM_INSTRUCTION,
        response_model=EvidenceBatchOutput,
        render_item=lambda u: f"=== {u.source.title} ===\n{u.chunk_text}",
        static_overhead_text=static_overhead,
    )

    for batch in batches:
        evidence = _extract_from_batch(batch, plan, concepts, client, status, depth=0)
        on_batch_done([u.unit_id for u in batch], evidence)


def _extract_from_batch(
    units: list[ExtractionUnit],
    plan: Plan,
    concepts: str,
    client: LLMClient,
    status: TaskStatus,
    depth: int,
) -> list[Evidence]:
    if not units:
        return []

    listing = "\n\n".join(
        f"=== Единица [{i}] (источник: {u.source.title!r}, URL: {u.source.url}) ===\n{u.chunk_text}"
        for i, u in enumerate(units)
    )
    prompt = (
        f"Тема исследования: {plan.topic_title}\n"
        f"Известные подтемы/концепции: {concepts}\n\n"
        f"Единицы текста для анализа:\n\n{listing}\n\n"
        "Извлеки факты/определения/тезисы из ВСЕХ единиц выше, привязывая "
        "каждый к наиболее подходящей концепции из списка (или сформулируй "
        "свою, если ни одна не подходит). Обязательно укажи unit_index для "
        "каждого факта — номер единицы в квадратных скобках выше."
    )

    try:
        output: EvidenceBatchOutput = client.generate_structured(
            role="extractor_critic",
            prompt=prompt,
            response_model=EvidenceBatchOutput,
            status=status,
            system_instruction=SYSTEM_INSTRUCTION,
        )
    except GroqPromptTooLargeError:
        if len(units) <= 1:
            logger.warning(
                "Единица %s не влезает в TPM-бюджет даже одна в батче — пропускаем.",
                units[0].unit_id,
            )
            return []
        return _split_batch_and_retry(units, plan, concepts, client, status, depth)
    except GroqSchemaError as exc:
        if depth >= _MAX_SPLIT_DEPTH or len(units) <= 1:
            logger.warning(
                "Пропускаем батч из %d единиц: модель вернула невалидный JSON "
                "даже после repair и повторных попыток (%s).", len(units), exc,
            )
            return []
        logger.info(
            "GroqSchemaError на батче из %d единиц — делим батч пополам и "
            "повторяем (depth=%d).", len(units), depth,
        )
        return _split_batch_and_retry(units, plan, concepts, client, status, depth + 1)

    return _to_evidence_list(output, units)


def _split_batch_and_retry(
    units: list[ExtractionUnit],
    plan: Plan,
    concepts: str,
    client: LLMClient,
    status: TaskStatus,
    depth: int,
) -> list[Evidence]:
    mid = len(units) // 2
    left = _extract_from_batch(units[:mid], plan, concepts, client, status, depth)
    right = _extract_from_batch(units[mid:], plan, concepts, client, status, depth)
    return left + right


def _to_evidence_list(output: EvidenceBatchOutput, units: list[ExtractionUnit]) -> list[Evidence]:
    evidence_list: list[Evidence] = []
    id_by_index: dict[int, str] = {}
    for idx, item in enumerate(output.evidence):
        if 0 <= item.unit_index < len(units):
            source = units[item.unit_index].source
        else:
            logger.warning(
                "Модель вернула unit_index=%d вне диапазона [0, %d) — "
                "приписываем утверждение первой единице батча.",
                item.unit_index, len(units),
            )
            source = units[0].source
        ev = Evidence(
            concept=item.concept,
            statement=item.statement,
            source_id=source.source_id,
            confidence=item.confidence,
            is_definition=item.is_definition,
            critic_note=item.critic_note,
        )
        id_by_index[idx] = ev.evidence_id
        evidence_list.append(ev)

    for idx, item in enumerate(output.evidence):
        evidence_list[idx].contradicts = [
            id_by_index[i] for i in item.contradicts_indices if i in id_by_index and i != idx
        ]

    return evidence_list


def extract_evidence_from_source(
    source: SourceCandidate,
    plan: Plan,
    client: LLMClient,
    status: TaskStatus,
    max_chars: int = 8000,
) -> list[Evidence]:
    """Совместимость с прежним интерфейсом (используется в тестах/вне
    Orchestrator) — извлекает evidence из ОДНОГО источника, без
    межисточникового батчинга. Сам Orchestrator использует
    extract_evidence_from_sources() для нескольких источников сразу."""
    collected: list[Evidence] = []

    def _collect(_unit_ids: list[str], new_evidence: list[Evidence]) -> None:
        collected.extend(new_evidence)

    extract_evidence_from_sources(
        [source], plan, client, status,
        already_done_unit_ids=set(),
        on_batch_done=_collect,
        max_chars=max_chars,
    )
    return collected