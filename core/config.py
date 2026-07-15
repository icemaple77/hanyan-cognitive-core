"""Configuration for the HCC v2 core modules.

Every runtime knob is sourced from ``HCC_*`` environment variables through
Pydantic Settings so the modules behave identically whether they run as a
local process or inside a container.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    """Settings shared by the Redis, EventBus and QMD components.

    Attributes map to ``HCC_``-prefixed environment variables, e.g.
    ``redis_url`` -> ``HCC_REDIS_URL`` and ``qmd_dir`` -> ``HCC_QMD_DIR``.
    """

    model_config = SettingsConfigDict(
        env_prefix="HCC_",
        env_file=".env",
        extra="ignore",
    )

    # --- Redis working memory / event bus -------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used for working memory and Pub/Sub.",
    )
    redis_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the Redis backend (HCC_REDIS_ENABLED). When "
            "false, the EventBus falls back to an in-process, in-memory broker "
            "so the system runs with no external Redis dependency."
        ),
    )

    # Default TTLs (seconds) for the different working-memory categories.
    ttl_chat: int = Field(
        default=1800, ge=1, description="TTL for transient chat context (30 min)."
    )
    ttl_task: int = Field(
        default=3600, ge=1, description="TTL for in-flight task state (1 hour)."
    )
    ttl_prompt: int = Field(
        default=3600, ge=1, description="TTL for cached prompts (1 hour)."
    )
    ttl_embedding: int = Field(
        default=604800, ge=1, description="TTL for cached embeddings (7 days)."
    )

    # --- Event bus -------------------------------------------------------
    event_channel_prefix: str = Field(
        default="hcc:events",
        description="Redis channel namespace prefix for published events.",
    )
    event_source: str = Field(
        default="hcc",
        description="Default 'source' label stamped onto published events.",
    )

    # --- Query planner ---------------------------------------------------
    planner_model: str = Field(
        default="rule-based",
        description=(
            "Query-planner strategy selector (HCC_PLANNER_MODEL). The default "
            "'rule-based' planner needs no model and classifies queries via "
            "keyword heuristics."
        ),
    )

    # --- Context API defaults -------------------------------------------
    context_default_limit: int = Field(
        default=10,
        ge=1,
        description="Default per-provider item cap for the context API "
        "(HCC_CONTEXT_DEFAULT_LIMIT).",
    )
    context_max_limit: int = Field(
        default=50,
        ge=1,
        description="Upper bound clamped onto the requested context limit "
        "(HCC_CONTEXT_MAX_LIMIT).",
    )

    # --- QMD knowledge document generator -------------------------------
    qmd_dir: Path = Field(
        default=Path("./qmd"),
        description="Root output directory for generated knowledge documents.",
    )
    qmd_git_enabled: bool = Field(
        default=False,
        description="If true, auto git add+commit the QMD dir after generation.",
    )

    # --- Bidirectional sync engine --------------------------------------
    sync_interval: int = Field(
        default=300,
        ge=1,
        description=(
            "Seconds between sync passes when the SyncEngine runs as a loop "
            "(HCC_SYNC_INTERVAL)."
        ),
    )
    sync_git_enabled: bool = Field(
        default=False,
        description=(
            "If true, auto git add+commit the QMD dir after each sync pass "
            "(HCC_SYNC_GIT_ENABLED). Independent of HCC_QMD_GIT_ENABLED."
        ),
    )

    def ttl_for(self, category: str) -> int:
        """Return the default TTL (seconds) for a working-memory ``category``.

        Falls back to :attr:`ttl_chat` for unknown categories.
        """
        return {
            "chat": self.ttl_chat,
            "task": self.ttl_task,
            "prompt": self.ttl_prompt,
            "embedding": self.ttl_embedding,
        }.get(category, self.ttl_chat)


core_settings = CoreSettings()
