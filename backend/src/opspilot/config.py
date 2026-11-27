from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5433/opspilot"
    redis_url: str = "redis://localhost:6380/0"

    # A readiness check that takes longer than this counts as down
    health_check_timeout_seconds: float = 2.0

    # Creating a workspace needs this token. Unset means workspace creation is switched off.
    admin_token: SecretStr | None = None

    # Requests per minute for each API key, and workspace creations per hour for each IP
    api_rate_limit_per_minute: int = 120
    workspace_create_limit_per_hour: int = 10

    # Celery's queue. A separate Redis database from the app's cache and rate limits.
    celery_broker_url: str = "redis://localhost:6380/1"

    # Documents
    max_document_bytes: int = 1_000_000
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 50

    # Ingestion: give up after this many attempts; treat a document "processing" for longer
    # than stale_seconds as abandoned by a crashed worker; re-send documents still "pending"
    # after requeue_after_seconds (their task was lost, for example Redis restarted)
    ingestion_max_attempts: int = 3
    ingestion_stale_seconds: int = 600
    ingestion_requeue_after_seconds: int = 120

    # Embeddings. "hashing" needs no API key (word overlap only: tests, CI, offline work);
    # "gemini" uses Google's embedding model and needs GEMINI_API_KEY.
    embedding_provider: Literal["hashing", "gemini"] = "hashing"
    embedding_model: str = "gemini-embedding-001"
    gemini_api_key: SecretStr | None = None
    query_embedding_cache_seconds: int = 24 * 60 * 60

    # Search: how many candidates each retriever contributes before fusion, and the RRF
    # constant (60 is the value from the original paper; higher flattens rank differences)
    search_candidates: int = 50
    rrf_k: int = 60
    # A cross-encoder re-orders the top candidates (see search/rerank.py). Needs
    # `uv sync --extra rerank`; the model (80 MB) downloads on first use.
    reranker: Literal["none", "cross-encoder"] = "none"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    reranker_cache_dir: str | None = None
    rerank_candidates: int = 20

    # The investigation agent's language model. Unset GEMINI_API_KEY (or llm_provider "none")
    # switches investigations off; everything else still works.
    llm_provider: Literal["none", "gemini"] = "gemini"
    llm_model: str = "gemini-3.8-flash"
    # List prices (USD per million tokens) for cost tracking. Defaults: gemini-3.8-flash's
    # standard rate (introductory $0.75 / $3.75 until 2026-12-31; the free tier costs nothing,
    # but the figure still shows what the traffic would cost)
    llm_input_usd_per_mtok: float = 1.50
    llm_output_usd_per_mtok: float = 7.50
    # Tool-use rounds before the agent must answer with what it has
    agent_max_steps: int = 6
    # The reviewer can send a draft back for one revision
    agent_reflection: bool = True
    agent_timeout_seconds: float = 90.0
    # Streamed investigations send a comment this often, so proxies keep the connection open
    sse_heartbeat_seconds: float = 10.0
    # How the agent works. "react": reason → call a tool → repeat (quick questions).
    # "plan": plan the evidence → gather it in parallel → hypotheses → verify each → answer
    # (incidents). Either can be chosen per investigation.
    agent_strategy: Literal["react", "plan"] = "react"
    incident_agent_strategy: Literal["react", "plan"] = "plan"
    # Input + output tokens one run may spend. Past it, the agent stops gathering and answers
    # with what it has. 0 = no limit.
    agent_token_budget: int = 60_000
    # "plan": tools run at once per stage, each with its own time limit
    agent_max_parallel_tools: int = 6
    agent_tool_timeout_seconds: float = 20.0
    agent_max_hypotheses: int = 3
    # A run whose worker died is resumed from its last checkpoint, this many times in all
    agent_max_attempts: int = 2
    # Approved actions are sent to the workspace's "actions" webhook with this time limit
    action_webhook_timeout_seconds: float = 10.0
    # Serve the MCP tools at /mcp, for AI assistants such as Claude and Cursor
    mcp_enabled: bool = True
    investigations_per_hour: int = 30

    # Telemetry integrations (Prometheus, Loki, GitHub). Their tokens are encrypted with this
    # Fernet key; without it, integrations work only without tokens. Make one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    integrations_key: SecretStr | None = None
    # Integration URLs may point at localhost and private networks (a local Prometheus).
    # Unset means: allowed except in production, where it would let a workspace admin make
    # the server probe its own internal network (SSRF).
    integrations_allow_private_urls: bool | None = None
    telemetry_timeout_seconds: float = 10.0

    # New incidents at or above this severity get an investigation started automatically
    # ("none" switches that off)
    auto_investigate_severity: Literal["critical", "high", "medium", "low", "none"] = "high"

    @property
    def allow_private_integration_urls(self) -> bool:
        if self.integrations_allow_private_urls is not None:
            return self.integrations_allow_private_urls
        return self.environment != "production"

    @field_validator(
        "admin_token",
        "gemini_api_key",
        "integrations_key",
        "integrations_allow_private_urls",
        "reranker_cache_dir",
        mode="before",
    )
    @classmethod
    def _blank_secret_means_unset(cls, value: object) -> object:
        # ADMIN_TOKEN= in .env must switch creation off, not accept an empty header; and
        # GEMINI_API_KEY= (as in .env.example) must switch the agent off, not crash startup;
        # other blank values mean "use the default"
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
