"""
Orchestrator — не LLM. Чистый Python state machine, который:
- понимает пользовательский запрос (нормализует Task);
- вызывает роли в фиксированной последовательности;
- прокидывает структурированные данные между этапами;
- контролирует бюджет Gemini-вызовов и останавливается при исчерпании
  лимита (без fallback на платный tier);
- ПЕРСИСТИТ ПРОГРЕСС ПОСЛЕ КАЖДОГО ШАГА (см. orchestrator/checkpoint.py),
  чтобы задачу можно было продолжить позже командой `resume <task_id>`,
  не пересчитывая уже сделанную работу;
- никогда сам не пишет в реальный Vault (это staging/commit.py, только
  после явного approve).

RESEARCH_MODE (см. config/settings.py):
- "web" — прежний путь: Researcher (веб-поиск + fetch) → Extractor/Critic
  извлекает evidence из реального текста источников.
- "knowledge" (дефолт) — Researcher/Extractor полностью пропускаются;
  Elaborator (roles/elaborator.py) генерирует evidence по каждой подтеме
  плана из знаний модели, без сетевого I/O. Заметки в этом режиме
  помечаются frontmatter.source="model-knowledge" (см.
  roles/synthesizer_writer.py::_to_draft_note) — это ЧЕРНОВОЙ конспект без
  проверяемых источников, а не исследование.

ВАЖНО про бюджет при resume: MAX_GEMINI_CALLS_PER_TASK — это лимит на
ОДНУ СЕССИЮ/ПОПЫТКУ (status.gemini_calls_used обнуляется в начале каждого
вызова run(), в т.ч. при resume), а не на задачу за всё её время жизни.
Иначе после однократного исчерпания лимита задачу нельзя было бы
продолжить никогда. Накопительный расход по всем попыткам хранится
отдельно в TaskCheckpoint.total_gemini_calls_used — только для отчёта
пользователю.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from llm.factory import budget_limits_for_provider, create_llm_client, extraction_budget_limits, \
    create_extraction_llm_client
from orchestrator.budget import GeminiBudget, GeminiFreeLimitReached, GeminiTaskBudgetExceeded

from retrieval.search import VaultSearcher
from roles import critic, elaborator, extractor_critic, planner, researcher, synthesizer_writer, vault_analyst
from staging.changeset import save_changeset
from staging.checkpoint import save_checkpoint, delete_checkpoint, TaskCheckpoint, load_checkpoint
from storage.models import StagingChangeset, Task, TaskStatus
from tools.dedup import try_create_embedder
from tools.markdown_tools import slugify_filename
from validation import run_validation
from vault.db import VaultDB
from vault.index import VaultIndexer

logger = logging.getLogger(__name__)

# Пометка frontmatter.source (см. tools/markdown_tools.py) для заметок,
# написанных в RESEARCH_MODE=knowledge — без внешних проверяемых
# источников. Совпадает с roles.elaborator.MODEL_KNOWLEDGE_SOURCE_ID по
# смыслу, но это отдельная константа: одна — маркер source_id у Evidence
# (внутренний), другая — человекочитаемое значение во frontmatter заметки
# (видимое пользователю в Obsidian).
KNOWLEDGE_MODE_FRONTMATTER_SOURCE = "model-knowledge"


class OrchestratorStopped(Exception):
    """Управляемая остановка задачи (лимит бюджета/Gemini, либо ошибка
    resume — например, чекпоинт не найден). Прогресс сохранён (кроме
    случая, когда чекпоинт сам оказался нечитаем)."""

    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


@dataclass
class RunResult:
    task_id: str
    changeset: StagingChangeset | None
    status: TaskStatus
    stopped: bool
    message: str


class Orchestrator:
    def __init__(self, settings: Settings):
        settings.validate_free_only()
        settings.ensure_dirs()
        self.settings = settings

        self.db = VaultDB(settings.db_path)
        self.embedder = try_create_embedder(settings.embedding_model, settings.use_local_embeddings)

        rpm_limit, rpd_limit = budget_limits_for_provider(settings)
        self.budget = GeminiBudget(
            max_calls_per_task=settings.max_gemini_calls_per_task,
            rpm_soft_limit=rpm_limit,
            rpd_soft_limit=rpd_limit,
        )
        # Имя атрибута исторически "gemini", но фактический тип определяется
        # settings.llm_provider — roles/* работают с ним только через
        # generate_structured(...), тип провайдера им не важен.
        self.gemini = create_llm_client(settings, self.budget)
        # Отдельный клиент+бюджет для extraction/elaboration (см.
        # llm/factory.py) — на Groq использует другую модель
        # (compound-mini/gpt-oss-120b) с другим TPM/RPD, поэтому не может
        # делить лимитер с self.gemini. Используется как в web-режиме
        # (extractor_critic), так и в knowledge-режиме (elaborator) — в
        # обоих случаях это самый частый по числу вызовов шаг. Оба бюджета
        # читают и пишут в один и тот же status.gemini_calls_used
        # (передаётся при каждом вызове run()), так что
        # MAX_GEMINI_CALLS_PER_TASK продолжает работать как ОБЩИЙ потолок
        # на задачу независимо от того, какой из двух клиентов расходует
        # вызовы.
        ext_rpm, ext_rpd = extraction_budget_limits(settings)
        self.extraction_budget = GeminiBudget(
            max_calls_per_task=settings.max_gemini_calls_per_task,
            rpm_soft_limit=ext_rpm,
            rpd_soft_limit=ext_rpd,
        )
        self.extraction_client = create_extraction_llm_client(settings, self.extraction_budget)

    def close(self) -> None:
        self.db.close()

    # -- публичный API ------------------------------------------------

    def sync_vault_index(self) -> dict:
        """Шаг 8 ТЗ: анализ существующего Vault. Инкрементально, без LLM."""
        indexer = VaultIndexer(db=self.db, vault_path=self.settings.vault_path, embedder=self.embedder)
        stats = indexer.sync()
        indexer.resolve_wikilink_targets()
        return stats

    def run(
        self,
        raw_query: str | None = None,
        *,
        resume_task_id: str | None = None,
        progress_cb=None,
    ) -> RunResult:
        """
        Выполняет workflow до этапа STAGING. Два режима:

        - Новая задача: run(raw_query="...").
        - Продолжение остановленной задачи: run(resume_task_id="...").
          Уже завершённые шаги (согласно чекпоинту) пропускаются, бюджет
          Gemini-вызовов открывается заново на эту сессию.
        """

        def report(stage: str) -> None:
            if progress_cb:
                progress_cb(stage)

        checkpoint, task, status, base_total_calls = self._load_or_create_state(
            raw_query=raw_query, resume_task_id=resume_task_id, report=report,
        )

        def persist(stage_label: str) -> None:
            """Сохраняет чекпоинт немедленно после успешного завершения
            шага. Вызывается часто (в т.ч. внутри цикла extracting после
            КАЖДОГО источника/подтемы) — это и есть механизм resume."""
            checkpoint.last_completed_stage = stage_label
            checkpoint.status = status
            checkpoint.total_gemini_calls_used = base_total_calls + status.gemini_calls_used
            save_checkpoint(self.settings.checkpoint_dir, checkpoint)

        try:
            report("Анализ Vault (индексация)…")
            self.sync_vault_index()  # чистый код, без Gemini — безопасно повторять всегда

            # -- Planning ------------------------------------------------
            if checkpoint.plan is None:
                report("Планирование исследования (Gemini)…")
                status.stage = "planning"
                plan = planner.build_plan(task, self.gemini, status)
                checkpoint.plan = plan
                persist("planned")
            else:
                plan = checkpoint.plan
                report("План уже построен (из чекпоинта) — пропускаем.")

            # -- Researching / Fetching (только RESEARCH_MODE=web) --------
            if self.settings.research_mode == "web":
                if not checkpoint.raw_candidates_done:
                    report("Поиск источников (бесплатный веб-поиск)…")
                    raw_candidates = researcher.collect_raw_candidates(
                        plan,
                        max_results_per_query=self.settings.max_search_results_per_query,
                        max_sources_per_subtopic=self.settings.max_sources_per_subtopic,
                    )
                    checkpoint.raw_candidates = raw_candidates
                    checkpoint.raw_candidates_done = True
                    persist("raw_candidates_collected")
                else:
                    raw_candidates = checkpoint.raw_candidates
                    report("Сырые источники уже собраны (из чекпоинта) — пропускаем.")

                if not checkpoint.sources_selected_done:
                    report("Отбор релевантных источников (Gemini)…")
                    status.stage = "researching"
                    selected = researcher.select_relevant_sources(
                        raw_candidates,
                        self.gemini,
                        status,
                        max_per_subtopic=self.settings.max_sources_per_subtopic,
                    )
                    checkpoint.selected_sources = selected
                    checkpoint.sources_selected_done = True
                    persist("sources_selected")
                else:
                    selected = checkpoint.selected_sources
                    report("Источники уже отобраны (из чекпоинта) — пропускаем.")

                if not checkpoint.sources_fetched_done:
                    report("Загрузка и очистка текста источников…")
                    fetched = researcher.fetch_selected_sources(selected)
                    checkpoint.fetched_sources = fetched
                    checkpoint.sources_fetched_done = True
                    persist("sources_fetched")
                else:
                    fetched = checkpoint.fetched_sources
                    report("Тексты источников уже загружены (из чекпоинта) — пропускаем.")
            else:
                report(
                    "Knowledge-режим (RESEARCH_MODE=knowledge): веб-поиск и "
                    "загрузка источников пропущены."
                )
                fetched = []

            # -- Extracting / Elaborating ----------------------------------
            # Гранулярность resume — по отдельной единице: в web-режиме это
            # чанк источника (см. roles/extractor_critic.py::ExtractionUnit),
            # в knowledge-режиме — целая подтема плана (см.
            # roles/elaborator.py). Поле checkpoint.extracted_unit_ids
            # переиспользуется для ОБОИХ режимов как список строковых
            # идентификаторов уже обработанных единиц — семантика
            # конкретного идентификатора зависит от режима, но структура
            # чекпоинта (list[str]) от этого не меняется, поэтому отдельное
            # поле не заводилось.
            if not checkpoint.extraction_done:
                status.stage = "extracting"
                evidence: list = list(checkpoint.evidence)
                already_done = set(checkpoint.extracted_unit_ids)

                if self.settings.research_mode == "web":
                    report("Извлечение фактов и проверка по источникам (Gemini/Groq)…")

                    def _on_batch_done(unit_ids: list[str], new_evidence: list) -> None:
                        evidence.extend(new_evidence)
                        checkpoint.evidence = evidence
                        checkpoint.extracted_unit_ids.extend(unit_ids)
                        persist("extracting")

                    extractor_critic.extract_evidence_from_sources(
                        fetched, plan, self.extraction_client, status,
                        already_done_unit_ids=already_done,
                        on_batch_done=_on_batch_done,
                        max_units_per_source=self.settings.max_chunks_per_source,
                    )
                else:
                    report("Раскрытие подтем из знаний модели (Gemini/Groq)…")

                    def _on_batch_done(subtopic_titles: list[str], new_evidence: list) -> None:
                        evidence.extend(new_evidence)
                        checkpoint.evidence = evidence
                        checkpoint.extracted_unit_ids.extend(subtopic_titles)
                        persist("extracting")

                    elaborator.elaborate_subtopics(
                        plan, self.extraction_client, status,
                        already_done_subtopic_titles=already_done,
                        on_batch_done=_on_batch_done,
                    )

                checkpoint.extraction_done = True
                persist("extraction_done")
            else:
                evidence = checkpoint.evidence
                report("Факты уже получены (из чекпоинта) — пропускаем.")

            # -- Vault analysis --------------------------------------------
            if not checkpoint.vault_analysis_done:
                report(
                    "Анализ существующих заметок Vault "
                    "(локально + Gemini для спорных случаев)…"
                )
                status.stage = "vault_analysis"
                searcher = VaultSearcher(self.db, embedder=self.embedder)
                existing_notes = vault_analyst.find_existing_notes_for_plan(
                    plan,
                    evidence,
                    searcher,
                    self.gemini,
                    status,
                    high_threshold=self.settings.dedup_high_threshold,
                    low_threshold=self.settings.dedup_low_threshold,
                )
                checkpoint.existing_notes = existing_notes
                checkpoint.vault_analysis_done = True
                persist("vault_analysis_done")
            else:
                existing_notes = checkpoint.existing_notes
                report("Анализ Vault уже выполнен (из чекпоинта) — пропускаем.")

            # -- Synthesis (planning) + Writer + Critic, map-reduce ---------
            if not checkpoint.synthesis_done:
                status.stage = "synthesizing"

                # Папка для заметок этой задачи: переиспользуем существующую
                # структуру папок Vault там, где это уместно (см.
                # PLAN_SYSTEM_INSTRUCTION), а иначе — тематическая подпапка
                # под default_notes_folder, а не сам default_notes_folder
                # "плоско" на все темы подряд.
                existing_folders = self.db.get_distinct_folders()
                topic_folder = (
                    f"{self.settings.default_notes_folder}/{slugify_filename(plan.topic_title)}"
                ).strip("/")

                if not checkpoint.note_plan_done:
                    report("Планирование структуры заметок (Gemini)…")
                    note_plan = synthesizer_writer.plan_notes(
                        plan, evidence, existing_notes, self.gemini, status,
                        existing_folders=existing_folders, default_folder=topic_folder,
                        max_notes=self.settings.max_notes_per_task,
                    )
                    checkpoint.note_plan = note_plan
                    checkpoint.note_plan_done = True
                    persist("note_plan_done")
                else:
                    note_plan = checkpoint.note_plan
                    report("План заметок уже построен (из чекпоинта) — пропускаем.")

                known_titles, title_map = synthesizer_writer.prepare_linking_context(
                    note_plan, existing_notes
                )
                already_written = set(checkpoint.written_note_indices)
                drafts = list(checkpoint.drafts)
                remaining_items = [
                    (i, item) for i, item in enumerate(note_plan.notes) if i not in already_written
                ]
                if remaining_items and already_written:
                    report(
                        f"Пропускаем {len(note_plan.notes) - len(remaining_items)} уже "
                        "написанных заметок (из чекпоинта)."
                    )

                mark_source = (
                    KNOWLEDGE_MODE_FRONTMATTER_SOURCE
                    if self.settings.research_mode == "knowledge"
                    else None
                )
                for i, item in remaining_items:
                    report(f"Написание заметки «{item.title}» (Gemini)…")
                    draft = critic.run_critic_cycle(
                        item, evidence, known_titles, title_map, fetched,
                        self.gemini, status, default_folder=topic_folder,
                        max_rounds=self.settings.max_critic_rounds,
                        mark_source=mark_source,
                    )
                    if draft.needs_review:
                        report(
                            f"⚠ Критик не одобрил заметку «{item.title}» после "
                            f"{draft.critic_rounds} попыт(ки/ок) — сохранена как есть."
                        )
                    drafts.append(draft)
                    checkpoint.drafts = drafts
                    checkpoint.written_note_indices.append(i)
                    # Персист ПОСЛЕ КАЖДОЙ заметки (включая критик-раунды
                    # внутри неё) — именно на этом шаге теперь самый частый
                    # риск упереться в бюджет вызовов.
                    persist("synthesizing")

                checkpoint.relationships = synthesizer_writer.build_relationships(drafts)
                checkpoint.synthesis_done = True
                persist("synthesis_done")
                relationships = checkpoint.relationships
            else:
                drafts = checkpoint.drafts
                relationships = checkpoint.relationships
                report("Синтез уже выполнен (из чекпоинта) — пропускаем.")

            # -- Validation + Staging (без Gemini, всегда выполняются заново,
            # т.к. дёшевы и должны учитывать текущее состояние db/Vault) ----
            report("Валидация предложенных изменений…")
            status.stage = "validating"
            creates = [d for d in drafts if d.action.value == "create"]
            updates = [d for d in drafts if d.action.value == "update"]
            changeset = StagingChangeset(
                task_id=task.task_id,
                creates=creates,
                updates=updates,
                deletes=[],
                relationships=relationships,
            )
            changeset.validation = run_validation(changeset, self.db, self.settings.allow_delete)

            report("Сохранение в staging (Vault пока не тронут)…")
            status.stage = "staged"
            save_changeset(self.settings.staging_dir, changeset)

            # Задача успешно доведена до staging — чекпоинт больше не нужен:
            # дальнейшее состояние живёт в StagingChangeset, а не в нём.
            delete_checkpoint(self.settings.checkpoint_dir, task.task_id)

            status.finished = True
            return RunResult(
                task_id=task.task_id,
                changeset=changeset,
                status=status,
                stopped=False,
                message="Изменения подготовлены и ждут вашего approve.",
            )

        except (GeminiFreeLimitReached, GeminiTaskBudgetExceeded) as exc:
            status.stage = "stopped"
            status.stopped_reason = str(exc)
            # Обновляем сохранённый статус/накопленный счётчик даже если
            # новый шаг целиком не завершился (например, упали в середине
            # planning) — last_completed_stage при этом не меняется,
            # т.к. persist() вызывается с уже известной меткой шага.
            persist(checkpoint.last_completed_stage)
            logger.warning("Задача %s остановлена: %s", task.task_id, exc)
            return RunResult(
                task_id=task.task_id,
                changeset=None,
                status=status,
                stopped=True,
                message=(
                    f"{exc}\n\n"
                    f"Прогресс сохранён (шаг: {checkpoint.last_completed_stage}). "
                    f"Продолжить: python -m cli.main resume {task.task_id}"
                ),
            )

    # -- внутреннее ------------------------------------------------------

    def _load_or_create_state(
        self,
        *,
        raw_query: str | None,
        resume_task_id: str | None,
        report,
    ) -> tuple[TaskCheckpoint, Task, TaskStatus, int]:
        if resume_task_id:
            checkpoint = load_checkpoint(self.settings.checkpoint_dir, resume_task_id)
            if checkpoint is None:
                raise OrchestratorStopped(
                    f"Чекпоинт для задачи {resume_task_id!r} не найден, повреждён "
                    "или относится к несовместимой версии — продолжить нельзя. "
                    "Запустите задачу заново командой 'ask'.",
                    task_id=resume_task_id,
                )
            # ВАЖНО: предполагается, что storage.models.Task допускает
            # явную передачу task_id (обычное pydantic-поле, а не
            # read-only/frozen с default_factory=uuid). Если это не так —
            # замените на task = Task(raw_query=...); task.task_id = checkpoint.task_id
            # (сработает, если модель не frozen), либо явно откройте
            # task_id как параметр конструктора в storage/models.py.
            task = Task(
                task_id=checkpoint.task_id,
                raw_query=checkpoint.raw_query,
                language=checkpoint.language,
            )
            status = TaskStatus(task_id=task.task_id, stage=checkpoint.last_completed_stage)
            base_total_calls = checkpoint.total_gemini_calls_used
            report(
                f"Продолжаем задачу {task.task_id} "
                f"(последний завершённый шаг: «{checkpoint.last_completed_stage}», "
                f"уже потрачено Gemini-вызовов всего: {base_total_calls})…"
            )
            return checkpoint, task, status, base_total_calls

        if not raw_query:
            raise ValueError("raw_query обязателен для новой задачи (resume_task_id не передан)")

        task = Task(raw_query=raw_query, language=self.settings.language)
        status = TaskStatus(task_id=task.task_id, stage="started")
        checkpoint = TaskCheckpoint(
            task_id=task.task_id,
            raw_query=task.raw_query,
            language=task.language,
            status=status,
        )
        save_checkpoint(self.settings.checkpoint_dir, checkpoint)
        return checkpoint, task, status, 0