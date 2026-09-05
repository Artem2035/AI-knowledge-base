"""
Orchestrator — не LLM. Чистый Python state machine, который:
- понимает пользовательский запрос (нормализует Task);
- вызывает роли в фиксированной последовательности;
- прокидывает структурированные данные между этапами;
- контролирует бюджет Gemini-вызовов и останавливается при исчерпании
  лимита (без fallback на платный tier);
- персистит прогресс, чтобы задачу можно было продолжить позже;
- никогда сам не пишет в реальный Vault (это staging/commit.py, только
  после явного approve).
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
from storage.models import StagingChangeset, Task, TaskStatus
from tools.dedup import try_create_embedder
from validation import run_validation
from vault.db import VaultDB
from vault.index import VaultIndexer

logger = logging.getLogger(__name__)


class OrchestratorStopped(Exception):
    """Управляемая остановка задачи (лимит бюджета/Gemini). Прогресс сохранён."""

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

    def run(self, raw_query: str, progress_cb=None) -> RunResult:
        """
        Выполняет полный workflow до этапа STAGING (не коммитит в Vault —
        это делает CLI отдельным вызовом staging.commit после approve).
        """

        def report(stage: str) -> None:
            if progress_cb:
                progress_cb(stage)

        task = Task(raw_query=raw_query, language=self.settings.language)
        status = TaskStatus(task_id=task.task_id, stage="started")

        try:
            report("Анализ Vault (индексация)…")
            self.sync_vault_index()

            report("Планирование исследования (Gemini)…")
            status.stage = "planning"
            plan = planner.build_plan(task, self.gemini, status)

            report("Поиск источников (бесплатный веб-поиск)…")
            status.stage = "researching"
            raw_candidates = researcher.collect_raw_candidates(
                plan,
                max_results_per_query=self.settings.max_search_results_per_query,
                max_sources_per_subtopic=self.settings.max_sources_per_subtopic,
            )

            report("Отбор релевантных источников (Gemini)…")
            selected = researcher.select_relevant_sources(
                raw_candidates,
                self.gemini,
                status,
                max_per_subtopic=self.settings.max_sources_per_subtopic,
            )

            report("Загрузка и очистка текста источников…")
            fetched = researcher.fetch_selected_sources(selected)

            report("Извлечение фактов и проверка (Gemini)…")
            status.stage = "extracting"
            evidence: list = []
            for source in fetched:
                evidence.extend(
                    extractor_critic.extract_evidence_from_source(source, plan, self.gemini, status)
                )

            report("Анализ существующих заметок Vault (локально + Gemini для спорных случаев)…")
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
            logger.warning("Задача %s остановлена: %s", task.task_id, exc)
            return RunResult(
                task_id=task.task_id,
                changeset=None,
                status=status,
                stopped=True,
                message=str(exc),
            )
