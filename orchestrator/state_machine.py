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
from llm.factory import budget_limits_for_provider, create_llm_client
from orchestrator.budget import GeminiBudget, GeminiFreeLimitReached, GeminiTaskBudgetExceeded

from retrieval.search import VaultSearcher
from roles import extractor_critic, planner, researcher, synthesizer_writer, vault_analyst
from staging.changeset import save_changeset
from staging.checkpoint import save_checkpoint, delete_checkpoint, TaskCheckpoint, load_checkpoint
from storage.models import StagingChangeset, Task, TaskStatus
from tools.dedup import try_create_embedder
from validation import run_validation
from vault.db import VaultDB
from vault.index import VaultIndexer

logger = logging.getLogger(__name__)


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
            КАЖДОГО источника) — это и есть механизм resume."""
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

            # -- Researching: сбор сырых кандидатов (без Gemini) ---------
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

            # -- Researching: отбор релевантных (1 Gemini call) ----------
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

            # -- Fetching (без Gemini) ------------------------------------
            if not checkpoint.sources_fetched_done:
                report("Загрузка и очистка текста источников…")
                fetched = researcher.fetch_selected_sources(selected)
                checkpoint.fetched_sources = fetched
                checkpoint.sources_fetched_done = True
                persist("sources_fetched")
            else:
                fetched = checkpoint.fetched_sources
                report("Тексты источников уже загружены (из чекпоинта) — пропускаем.")

            # -- Extracting: самый "дорогой" шаг — гранулярность по источнику
            if not checkpoint.extraction_done:
                report("Извлечение фактов и проверка (Gemini)…")
                status.stage = "extracting"
                already_done = set(checkpoint.extracted_source_ids)
                evidence: list = list(checkpoint.evidence)
                remaining = [s for s in fetched if s.source_id not in already_done]
                if remaining and already_done:
                    report(
                        f"Пропускаем {len(fetched) - len(remaining)} уже "
                        "обработанных источников (из чекпоинта)."
                    )
                for source in remaining:
                    source_evidence = extractor_critic.extract_evidence_from_source(
                        source, plan, self.gemini, status
                    )
                    evidence.extend(source_evidence)
                    checkpoint.evidence = evidence
                    checkpoint.extracted_source_ids.append(source.source_id)
                    # Персист ПОСЛЕ КАЖДОГО источника — именно здесь чаще
                    # всего происходит остановка по бюджету, и именно тут
                    # resume даёт наибольший выигрыш.
                    persist("extracting")
                checkpoint.extraction_done = True
                persist("extraction_done")
            else:
                evidence = checkpoint.evidence
                report("Факты уже извлечены (из чекпоинта) — пропускаем.")

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

            # -- Synthesis + Writer -----------------------------------------
            if not checkpoint.synthesis_done:
                report("Синтез структуры знаний и написание заметок (Gemini)…")
                status.stage = "synthesizing"
                drafts, relationships = synthesizer_writer.synthesize_and_write(
                    plan,
                    evidence,
                    fetched,
                    existing_notes,
                    self.gemini,
                    status,
                    default_folder=self.settings.default_notes_folder,
                )
                checkpoint.drafts = drafts
                checkpoint.relationships = relationships
                checkpoint.synthesis_done = True
                persist("synthesis_done")
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