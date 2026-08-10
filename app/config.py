from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "SmartReco"
    app_env: str = "development"
    app_secret: str = "development-only-change-me-please-32-chars"
    database_url: str = "sqlite:///./.smartreco/smartreco.db"
    qdrant_path: str = "./.smartreco/qdrant"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "smartreco-products"

    mesh_api_key: str | None = None
    mesh_base_url: str = "https://api.meshapi.ai/v1"
    mesh_free_model: str = "minimax/m2-her"
    mesh_paid_model: str = "openai/gpt-4o-mini"
    mesh_premium_model: str = "openai/gpt-5.4-mini"
    mesh_embedding_model: str = "openai/text-embedding-3-small"
    mesh_embeddings_enabled: bool = False
    mesh_model_mode: str = "free"
    mesh_failover_models: str = "minimax/m2-her,tencent/hy3,openai/gpt-4o-mini"
    mesh_timeout_seconds: int = 90
    mesh_contextual_timeout_seconds: int = 25
    mesh_sdk_max_retries: int = 1

    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "smartreco"
    langsmith_reconciliation_seconds: int = 30
    langsmith_export_delay_seconds: int = 120
    langsmith_reconciliation_days: int = 7

    recommendation_min_trigger_score: float = 8.0
    recommendation_cooldown_seconds: int = 20
    contextual_recommendation_ttl_hours: int = 24
    signal_debounce_seconds: int = 5
    session_ttl_hours: int = 8
    session_idle_minutes: int = 30
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    registration_email_domain: str = "smartreco.ai"
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    scheduler_enabled: bool = True

    demo_admin_email: str = "admin@smartreco.local"
    demo_admin_password: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "SmartReco <recommendations@smartreco.local>"
    delivery_mode: str = "sandbox"
    delivery_max_attempts: int = 4
    delivery_retry_base_seconds: int = 60
    delivery_overdue_hours: int = 24
    digest_hour_local: int = 15
    app_public_url: str = "http://127.0.0.1:8000"

    activity_retention_days: int = 180
    signal_retention_days: int = 30
    recommendation_retention_days: int = 90
    auth_attempt_retention_days: int = 30
    mcp_trusted_local_only: bool = True

    cookie_secure: bool = Field(default=False)

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def langsmith_connected(self) -> bool:
        return bool(self.langsmith_tracing and self.langsmith_api_key)

    def validate_production_security(self) -> None:
        if self.app_env.lower() != "production":
            return
        if self.app_secret == "development-only-change-me-please-32-chars" or len(self.app_secret) < 32:
            raise ValueError("APP_SECRET must be a unique value of at least 32 characters in production")
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        if not self.mesh_embeddings_enabled or not self.mesh_api_key:
            raise ValueError("Production semantic RAG requires Mesh embeddings through MESH_API_KEY")

    @property
    def active_chat_model(self) -> str:
        return {
            "free": self.mesh_free_model,
            "paid": self.mesh_paid_model,
            "premium": self.mesh_premium_model,
        }.get(self.mesh_model_mode, self.mesh_free_model)

    @property
    def model_failover_chain(self) -> list[str]:
        """Return at most three unique Mesh models, with the explicitly active model first."""
        configured = [model.strip() for model in self.mesh_failover_models.split(",") if model.strip()]
        ordered: list[str] = []
        for model in [self.active_chat_model, *configured]:
            if model not in ordered:
                ordered.append(model)
        return ordered[:3]

    def ensure_runtime_dirs(self) -> None:
        Path(".smartreco").mkdir(parents=True, exist_ok=True)
        if not self.qdrant_url:
            Path(self.qdrant_path).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production_security()
    settings.ensure_runtime_dirs()
    return settings
