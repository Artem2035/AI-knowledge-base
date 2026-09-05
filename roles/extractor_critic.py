from __future__ import annotations

import logging

from gemini.client import GeminiClient
from gemini.schemas import EvidenceBatchOutput
from llm.groq_client import GroqPromptTooLargeError, GroqSchemaError, _estimate_tokens
from storage.models import Evidence, Plan, SourceCandidate, TaskStatus

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Ты одновременно выполняешь две роли: Extractor и Critic/Fact-Checker. "
    "Extractor: извлеки из текста источника факты, определения, ключевые "
    "тезисы и аргументы, относящиеся к теме исследования. Каждое утверждение "
    "должно быть атомарным (одна мысль) и привязано к одной из заданных "
    "концепций/подтем. Critic: для каждого утверждения оцени confidence "
    "(0-1) по качеству источника и ясности формулировки; если утверждение "
    "выглядит непроверенным, спорным или противоречит здравому смыслу — "
    "снизь confidence и опиши это в critic_note. Не включай маркетинговые "
    "утверждения и voda без фактического содержания."
)

# Минимальный размер чанка, ниже которого дальше делить нет смысла —
# если модель не справляется даже с таким куском, просто пропускаем его
# с предупреждением, а не уходим в бесконечную рекурсию.
_MIN_CHUNK_CHARS = 1200
# Сколько раз пробуем бисекцию чанка при GroqSchemaError, прежде чем сдаться.
_MAX_SPLIT_DEPTH = 2


def extract_evidence_from_source(
    source: SourceCandidate,
    plan: Plan,
    client: GeminiClient,
    status: TaskStatus,
    max_chars: int = 8000,
) -> list[Evidence]:
    """Извлекает evidence из одного источника.

    ИЗМЕНЕНИЯ (v2): текст источника больше не режется один раз статичным
    max_chars — вместо этого он делится на чанки под фактический доступный
    TPM-бюджет клиента (available_prompt_budget_tokens), чтобы не ловить
    413 "Request too large". На каждый чанк — отдельный вызов
    generate_structured; при GroqSchemaError (модель вернула невалидный JSON
    даже после repair) чанк делится пополам и повторяется — обычно это
    помогает, т.к. более короткий ответ реже "ломается" в середине
    генерации.

    Примечание: contradicts_indices разрешаются только внутри одного чанка/
    вызова (как и раньше) — сквозной анализ противоречий между чанками
    и источниками остаётся будущим улучшением, не входит в этот MVP-фикс.
    """
    if not source.fetched_text:
        return []

    concepts = ", ".join(s.title for s in plan.subtopics)
    full_text = source.fetched_text[:max_chars]

    chunk_char_budget = _resolve_chunk_char_budget(
        client=client,
        plan=plan,
        source=source,
        concepts=concepts,
        fallback_max_chars=max_chars,
    )

    chunks = _split_text(full_text, chunk_char_budget)

    evidence_list: list[Evidence] = []
    for chunk in chunks:
        evidence_list.extend(
            _extract_from_chunk(
                chunk_text=chunk,
                source=source,
                plan=plan,
                concepts=concepts,
                client=client,
                status=status,
                depth=0,
            )
        )
    return evidence_list


def _resolve_chunk_char_budget(
    *,
    client: GeminiClient,
    plan: Plan,
    source: SourceCandidate,
    concepts: str,
    fallback_max_chars: int,
) -> int:
    """Спрашивает у клиента доступный token-бюджет под prompt (если клиент
    его поддерживает — т.е. это GroqClient) и переводит его в символы.
    Для GeminiClient (нет такого метода) просто используем старый
    fallback_max_chars, т.к. у Gemini свои, гораздо более щедрые лимиты."""
    budget_fn = getattr(client, "available_prompt_budget_tokens", None)
    if budget_fn is None:
        return fallback_max_chars

    # Промпт = тема + concepts + сам текст. Оцениваем накладные расходы
    # (всё, кроме текста источника) отдельно, чтобы вычесть их из бюджета.
    overhead_text = (
        f"Тема исследования: {plan.topic_title}\n"
        f"Известные подтемы/концепции: {concepts}\n\n"
        f"Текст источника (URL: {source.url}, заголовок: {source.title}):\n\n"
        "\n\n"
        "Извлеки факты/определения/тезисы, привязывая каждый к наиболее "
        "подходящей концепции из списка выше (или сформулируй свою, если "
        "ни одна не подходит)."
    )
    available_tokens = budget_fn(SYSTEM_INSTRUCTION, EvidenceBatchOutput)
    overhead_tokens = _estimate_tokens(overhead_text)
    text_budget_tokens = max(available_tokens - overhead_tokens, 0)

    # Консервативная оценка chars/token для кириллицы ~2.3 (см. groq_client).
    chunk_chars = int(text_budget_tokens * 2.0)  # доп. запас 15%
    if chunk_chars <= 0:
        logger.warning(
            "Доступный TPM-бюджет для источника %s близок к нулю — "
            "используем минимальный чанк %d символов.",
            source.url, _MIN_CHUNK_CHARS,
        )
        return _MIN_CHUNK_CHARS

    return max(min(chunk_chars, fallback_max_chars), _MIN_CHUNK_CHARS)


def _split_text(text: str, chunk_chars: int) -> list[str]:
    """Режет текст на чанки примерно по chunk_chars символов, стараясь
    не рвать предложения посередине (режем по ближайшему переводу строки
    или точке перед лимитом, если он найден в разумных пределах)."""
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            # ищем ближайшую границу предложения/абзаца в последних 20%
            # окна, чтобы не резать посреди фразы
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


def _extract_from_chunk(
    *,
    chunk_text: str,
    source: SourceCandidate,
    plan: Plan,
    concepts: str,
    client: GeminiClient,
    status: TaskStatus,
    depth: int,
) -> list[Evidence]:
    prompt = (
        f"Тема исследования: {plan.topic_title}\n"
        f"Известные подтемы/концепции: {concepts}\n\n"
        f"Текст источника (URL: {source.url}, заголовок: {source.title}):\n\n"
        f"{chunk_text}\n\n"
        "Извлеки факты/определения/тезисы, привязывая каждый к наиболее "
        "подходящей концепции из списка выше (или сформулируй свою, если "
        "ни одна не подходит)."
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
        # Наша оценка бюджета уже должна была это предотвратить, но на
        # всякий случай — если чанк всё же оказался слишком большим,
        # делим его и пробуем снова (не увеличивая depth-лимит retry,
        # т.к. это не ошибка качества ответа, а вопрос размера).
        if len(chunk_text) <= _MIN_CHUNK_CHARS:
            logger.warning(
                "Чанк источника %s слишком велик для TPM-бюджета даже на "
                "минимальном размере — пропускаем.", source.url,
            )
            return []
        return _split_and_retry(
            chunk_text, source, plan, concepts, client, status, depth
        )
    except GroqSchemaError as exc:
        if depth >= _MAX_SPLIT_DEPTH or len(chunk_text) <= _MIN_CHUNK_CHARS:
            logger.warning(
                "Пропускаем чанк источника %s: модель вернула невалидный "
                "JSON даже после repair и повторных попыток (%s).",
                source.url, exc,
            )
            return []
        logger.info(
            "GroqSchemaError на чанке источника %s — делим чанк пополам и "
            "повторяем (depth=%d).", source.url, depth,
        )
        return _split_and_retry(
            chunk_text, source, plan, concepts, client, status, depth
        )

    return _to_evidence_list(output, source)


def _split_and_retry(
    chunk_text: str,
    source: SourceCandidate,
    plan: Plan,
    concepts: str,
    client: GeminiClient,
    status: TaskStatus,
    depth: int,
) -> list[Evidence]:
    mid = len(chunk_text) // 2
    # Делим по ближайшему пробелу, чтобы не резать слово пополам
    split_at = chunk_text.rfind(" ", 0, mid)
    split_at = split_at if split_at > 0 else mid

    left, right = chunk_text[:split_at].strip(), chunk_text[split_at:].strip()

    result: list[Evidence] = []
    for sub_chunk in (left, right):
        if not sub_chunk:
            continue
        result.extend(
            _extract_from_chunk(
                chunk_text=sub_chunk,
                source=source,
                plan=plan,
                concepts=concepts,
                client=client,
                status=status,
                depth=depth + 1,
            )
        )
    return result


def _to_evidence_list(
    output: EvidenceBatchOutput, source: SourceCandidate
) -> list[Evidence]:
    evidence_list: list[Evidence] = []
    id_by_index: dict[int, str] = {}
    for idx, item in enumerate(output.evidence):
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

    # разрешаем contradicts_indices -> evidence_id (в рамках этого же батча/чанка)
    for idx, item in enumerate(output.evidence):
        evidence_list[idx].contradicts = [
            id_by_index[i] for i in item.contradicts_indices if i in id_by_index and i != idx
        ]

    return evidence_list