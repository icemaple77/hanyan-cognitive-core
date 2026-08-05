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
    ttl_emotion: int = Field(
        default=2592000,
        ge=1,
        description="TTL for the Redis hot emotion-state snapshot (30 days; "
        "emotion decays on a day-scale, not a chat-session scale, see "
        "docs/emotion-design.md 2.6).",
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
    sync_auto_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the gateway's built-in sync automation: the "
            "periodic sync_interval loop and the debounced store/update/delete "
            "event-triggered sync (HCC_SYNC_AUTO_ENABLED)."
        ),
    )

    # --- Dream engine (native three-phase consolidation, v2) ------------
    dream_auto_enabled: bool = Field(
        default=True,
        description="Master switch for the three background dream loops "
        "(HCC_DREAM_AUTO_ENABLED). Independent of HCC_SYNC_AUTO_ENABLED.",
    )
    dream_light_interval_hours: int = Field(
        default=6, ge=1, description="Hours between Light-phase runs (HCC_DREAM_LIGHT_INTERVAL_HOURS)."
    )
    dream_light_lookback_hours: int = Field(
        default=6, ge=1, description="Light phase scans Memory rows created within this many hours."
    )
    dream_rem_hour: int = Field(default=2, ge=0, le=23, description="REM phase daily trigger hour (local time).")
    dream_rem_minute: int = Field(default=30, ge=0, le=59, description="REM phase daily trigger minute.")
    dream_deep_hour: int = Field(default=3, ge=0, le=23, description="Deep phase daily trigger hour (local time).")
    dream_deep_minute: int = Field(default=0, ge=0, le=59, description="Deep phase daily trigger minute.")
    dream_rem_lookback_days: int = Field(default=7, ge=1, description="REM phase clustering window in days.")
    dream_rem_min_cluster_size: int = Field(
        default=3, ge=2, description="Minimum members for a REM tag-overlap cluster to count as a theme."
    )
    dream_min_score: float = Field(
        default=0.7, ge=0.0, description="Deep phase promotion score threshold (Phase-1 5-signal formula)."
    )
    dream_min_access_count: int = Field(default=3, ge=0, description="Deep phase minimum access_count to be eligible.")
    dream_max_age_days: int = Field(default=30, ge=1, description="Deep phase maximum memory age (days) to be eligible.")
    dream_recency_halflife_days: float = Field(
        default=14.0, gt=0, description="Half-life (days) for the recency component and the phase-boost decay."
    )
    dream_limit: int = Field(default=10, ge=1, description="Max memories promoted per Deep run.")
    dream_max_prior_loss_fraction: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Safety valve: skip updating an existing knowledge memory if the new cluster "
        "covers less than (1 - this) of its previously recorded source memories.",
    )
    dream_diary_dir: Path = Field(
        default=Path("~/workspace/AICore/Dreams"),
        description="Dual dream-diary output directory: 含烟梦境.md (narrative) + 深梦报告.md (audit) "
        "(HCC_DREAM_DIARY_DIR).",
    )

    # --- Obsidian vault: archive + per-agent export + browse API -------
    archive_dir: Path = Field(
        default=Path("~/workspace/AICore-Archive"),
        description="External (indexer-excluded) home for orphaned QMD documents — "
        "memories deleted/unshared since the last generation are moved here instead "
        "of left stale under HCC_QMD_DIR (HCC_ARCHIVE_DIR).",
    )
    agent_export_dir: Path = Field(
        default=Path("~/workspace/AICore/agents"),
        description="Root for the per-agent_id human-readable memory export "
        "(<dir>/<agent_id>/*.md), independent of QMDGenerator's shared=True filter "
        "(HCC_AGENT_EXPORT_DIR).",
    )
    vault_root: Path = Field(
        default=Path("~/workspace/AICore"),
        description="Obsidian vault root exposed read-only via GET /vault/list and "
        "/vault/read (HCC_VAULT_ROOT). Path traversal outside this root is rejected.",
    )

    # --- Emotion engine v2 (docs/emotion-design.md) ----------------------
    # Named-state thresholds (2.2) — initial proposal, not yet calibrated
    # against real conversation data; kept here (rather than hardcoded) so
    # they can be tuned via env/`.env` without a code change.
    emotion_attachment_closeness: float = Field(default=0.75, ge=0.0, le=1.0)
    emotion_attachment_happiness: float = Field(default=0.6, ge=0.0, le=1.0)
    emotion_elated_happiness: float = Field(default=0.75, ge=0.0, le=1.0)
    emotion_elated_curiosity: float = Field(default=0.6, ge=0.0, le=1.0)
    emotion_elated_fatigue_max: float = Field(default=0.3, ge=0.0, le=1.0)
    emotion_focused_focus: float = Field(default=0.75, ge=0.0, le=1.0)
    emotion_focused_fatigue_max: float = Field(default=0.5, ge=0.0, le=1.0)
    emotion_tired_fatigue: float = Field(default=0.7, ge=0.0, le=1.0)
    emotion_low_happiness_max: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_low_worry: float = Field(default=0.4, ge=0.0, le=1.0)
    emotion_worried_worry: float = Field(default=0.6, ge=0.0, le=1.0)
    emotion_curious_curiosity: float = Field(default=0.7, ge=0.0, le=1.0)
    emotion_curious_worry_max: float = Field(default=0.3, ge=0.0, le=1.0)

    # Retrieval mood-congruent weighting (2.3) — kept deliberately small so
    # emotion nudges ranking without overriding semantic relevance.
    emotion_retrieval_closeness_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    emotion_retrieval_worry_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    emotion_retrieval_closeness_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    emotion_retrieval_worry_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # Dream -> emotion baseline nudge (2.5) — fraction of the aggregate
    # T3-keyword delta from tonight's promoted memories that gets folded
    # into the decay-target anchor (not applied to current state directly).
    emotion_dream_baseline_weight: float = Field(default=0.3, ge=0.0, le=1.0)

    # --- Local noise filter (docs/local-noise-filter.md) ----------------
    # Async, event-driven review of low-trust memory writes (type=tool_result
    # or source=openclaw_plugin) via a local Ollama model — never blocks
    # /memory/store, see core/noise_filter_events.py.
    noise_filter_enabled: bool = Field(
        default=True,
        description="Master switch for async local-model noise review "
        "(HCC_NOISE_FILTER_ENABLED). Subscribes to MEMORY_CREATED; a low-value "
        "verdict soft-deletes (status='discarded'), never a hard delete.",
    )
    noise_filter_model: str = Field(
        default="qwen3.5:4b",
        description="Ollama model tag for noise review (HCC_NOISE_FILTER_MODEL). "
        "qwen3.5:4b scored 7/8 on the 8-sample validation run at ~1.76s/call warm "
        "(docs/local-noise-filter.md 一/二). think:false is mandatory — without it "
        "the model spends its whole output budget on hidden reasoning and never "
        "emits the JSON verdict.",
    )
    noise_filter_ollama_url: str = Field(
        default="http://localhost:11434",
        description="Ollama base URL for noise review (HCC_NOISE_FILTER_OLLAMA_URL). "
        "Independent of HCC_OLLAMA_URL (gateway/core/embeddings.py's plain "
        "os.getenv config), same default host.",
    )
    noise_filter_timeout: float = Field(
        default=15.0, gt=0,
        description="Per-call HTTP timeout in seconds against Ollama "
        "(HCC_NOISE_FILTER_TIMEOUT). Cold start (model swapped out) measured "
        "~3-4s, warm ~1.3-1.5s.",
    )
    noise_filter_concurrency: int = Field(
        default=4, ge=1,
        description="Concurrency cap used by scripts/noise_filter_backfill.py's "
        "Ollama calls (HCC_NOISE_FILTER_CONCURRENCY); measured ~0.8s/item "
        "effective throughput at 4 (docs/local-noise-filter.md 五).",
    )
    noise_filter_truncate_chars: int = Field(
        default=1500, ge=1,
        description="Content truncation length before sending to the model "
        "(HCC_NOISE_FILTER_TRUNCATE_CHARS). Matches the 8-sample validation run; "
        "not yet re-validated against the full tool_result content-length "
        "distribution (docs/local-noise-filter.md 三).",
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
            "emotion": self.ttl_emotion,
        }.get(category, self.ttl_chat)


core_settings = CoreSettings()
