"""
Единая точка конфигурации системы.

Все настройки читаются из переменных окружения / .env файла и НИКОГДА
не хардкодятся в коде ролей/инструментов. Это единственный модуль,
который знает про имена env-переменных.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Провайдер LLM ----
    # "gemini" | "groq" — единственная точка переключения, всё остальное
    # (roles/*, orchestrator) работает с любым провайдером одинаково,
    # т.к. оба клиента реализуют один и тот же метод generate_structured().
    llm_provider: str = Field(default="gemini")

    # ---- Gemini ----
    gemini_api_key: str = Field(default="", description="Ключ Gemini API (бесплатный тир)")
    gemini_model: str = Field(default="gemini-flash-latest")
    gemini_timeout_seconds: int = Field(default=60, ge=1)
    gemini_rpm_soft_limit: int = Field(default=8, ge=1)
    gemini_rpd_soft_limit: int = Field(default=200, ge=1)

    # ---- Groq ----
    groq_api_key: str = Field(default="", description="Ключ Groq API (бесплатный тир)")
    groq_model: str = Field(default="openai/gpt-oss-120b")
    groq_timeout_seconds: int = Field(default=60, ge=1)
    # Реальный лимит free tier у Groq выше (~30 RPM / ~14400 RPD), но
    # берём с запасом, чтобы не упираться в TPM-лимит на длинных промптах.
    groq_rpm_soft_limit: int = Field(default=25, ge=1)
    groq_rpd_soft_limit: int = Field(default=10000, ge=1)

    free_only: bool = Field(default=True, description="Жёсткий флаг: только бесплатные провайдеры")

    # Общий бюджет вызовов на задачу — не зависит от того, какой провайдер активен
    max_gemini_calls_per_task: int = Field(default=15, ge=1)
    max_gemini_retries: int = Field(default=3, ge=0)

    # ---- Vault ----
    vault_path: Path = Field(default=Path("./vault_placeholder"))
    vault_name: str = Field(default="MyVault")
    default_notes_folder: str = Field(default="Знания")

    # ---- Рабочие директории (строго вне Vault) ----
    workdir: Path = Field(default=Path("./.obsidian_ai_kb"))
    staging_dir: Path = Field(default=Path("./.obsidian_ai_kb/staging"))
    db_path: Path = Field(default=Path("./.obsidian_ai_kb/vault_index.sqlite3"))

    # ---- Retrieval / dedup ----
    use_local_embeddings: bool = Field(default=True)
    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    dedup_high_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    dedup_low_threshold: float = Field(default=0.55, ge=0.0, le=1.0)

    # ---- Прочее ----
    language: str = Field(default="ru")
    allow_delete: bool = Field(default=False)
    git_enabled: bool = Field(default=False)
    max_sources_per_subtopic: int = Field(default=4, ge=1)
    max_search_results_per_query: int = Field(default=6, ge=1)
    checkpoint_dir:Path = Field(default=Path("./staging/1"))

    @field_validator("vault_path", "workdir", "staging_dir", "db_path", mode="before")
    @classmethod
    def _expand(cls, v: str | Path) -> Path:
        return Path(v).expanduser()

    def ensure_dirs(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_free_only(self) -> None:
        """
        Жёсткая проверка режима FREE ONLY. Вызывается при старте Orchestrator.
        Ничего не "чинит" автоматически — если free_only=False, явно требуем
        подтверждения через отдельный флаг, чтобы платный режим никогда не
        включался случайно/по умолчанию.
        """
        if not self.free_only:
            raise RuntimeError(
                "FREE_ONLY=false запрещено в текущей версии MVP. "
                "Система спроектирована работать исключительно на бесплатном "
                "Gemini API. Платные провайдеры сознательно не реализованы."
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Только для тестов — сбросить закэшированные настройки."""
    global _settings
    _settings = None
