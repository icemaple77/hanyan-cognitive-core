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

    # --- Database / API 面(2026-09-03 由 gateway/core/config.py 合并进来)----
    database_url: str = Field(
        default="postgresql+asyncpg://hcc:hcc@localhost:5432/hcc",
        description="Postgres/pgvector DSN (HCC_DATABASE_URL).",
    )
    api_host: str = Field(default="0.0.0.0", description="Gateway 监听地址。")
    api_port: int = Field(default=8000, description="Gateway 监听端口。")
    debug: bool = Field(default=False, description="调试模式。")

    # --- Embedding:维度的唯一真相源 -------------------------------------
    # 2026-09-03 事故复盘:此前维度有两个互不相干的定义——gateway/models 硬编码
    # 1024 用于建表,gateway/core/embeddings.py 从 env 读(实际 768)用于产出向量。
    # 结果 documents 列是 1024、查询向量是 768,**每次语义检索都报 "different
    # vector dimensions",知识检索静默降级为纯 BM25**(公子抱怨的"Knowledge 全是
    # 技术旧档"的根因)。维度只许有这一个定义,建表与产出共用。
    # provider 默认值也从 "hash" 改为真实模型:hash 兜底会静默产生无意义向量,
    # 宁可在 .env 缺失时用对的模型,也不要悄悄写垃圾进库。
    embedding_provider: str = Field(
        default="sentence-transformers",
        description="嵌入后端:sentence-transformers | ollama | hash(仅测试)。",
    )
    embedding_model: str = Field(
        default="BAAI/bge-base-zh-v1.5", description="嵌入模型 id。"
    )
    embedding_dim: int = Field(
        default=768, ge=1,
        description="嵌入维度。**建表与运行时共用此值**,不得在别处硬编码。",
    )
    embedding_device: str = Field(default="cpu", description="sentence-transformers 设备。")
    embedding_query_instruction: str = Field(
        default="", description="BGE 非对称检索的 query 前缀指令(store 侧不加)。"
    )
    ollama_url: str = Field(
        default="http://localhost:11434", description="ollama 服务地址(HCC_OLLAMA_URL)。"
    )

    # --- Rerank(可选重排,默认关)-----------------------------------------
    rerank_enabled: bool = Field(default=False, description="是否启用 GGUF 重排。")
    rerank_model_path: Path = Field(
        default=Path("~/.cache/qmd/models/hf_ggml-org_qwen3-reranker-0.6b-q8_0.gguf").expanduser(),
        description="重排模型 GGUF 路径。",
    )
    rerank_n_ctx: int = Field(default=2048, ge=1, description="重排模型上下文长度。")

    # --- Session Harvester(各 runtime 会话收割)---------------------------
    harvester_enabled: bool = Field(default=True, description="是否开启会话收割循环。")
    harvest_interval: int = Field(default=60, ge=1, description="收割周期(秒)。")
    harvest_user_id: str = Field(default="michael", description="收割入库归属 user_id。")
    harvest_state: Path = Field(
        default=Path.home() / ".hcc" / "harvester_state.json",
        description="收割水位持久化路径。",
    )
    self_url: str = Field(
        default="http://127.0.0.1:8000/api/v1",
        description="进程内回调自身 API 的地址(收割器入库用)。",
    )

    # --- 文档索引增量同步(md 改动自动重新索引)---------------------------
    doc_index_enabled: bool = Field(
        default=True,
        description=(
            "是否开启文档增量索引循环。知识检索改走 documents 表后,若不自动检测"
            "文件变更就会静默服务过期内容——这个开关默认必须是开的。"
        ),
    )
    doc_index_interval: int = Field(
        default=60, ge=5,
        description=(
            "增量索引巡检周期(秒)。按 (mtime, 大小) 签名比对,未变的文件不读不算,"
            "一轮只有 stat 开销(实测 ~4ms/1124 文件),所以可以跑得很勤。"
        ),
    )

    # --- 注入(读路渲染)---------------------------------------------------
    inject_fragment_cap: int = Field(
        default=0, ge=0,
        description=(
            "每轮系统注入里允许的 harvester 原始对话碎片条数上限。默认 0——碎片是"
            "深挖检索池,不该霸占每轮注入位(保送席不受此限)。手感太薄可调到 3。"
        ),
    )

    # --- Agent 身份 -------------------------------------------------------
    agent_id: str = Field(default="default", description="本进程默认 agent_id(MCP 等)。")

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
    identity_aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Retrieval identity groups: primary user_id -> related user_ids that "
            "are all searched together when building /context (2026-08-09 排查 "
            "P1-3). 公子's memories are fragmented across scopes (michael + the "
            "Feishu/Hermes open_id ou_...), and strict per-scope search means "
            "cross-scope memories are never recalled. Listing them here lets one "
            "identity's memories surface for another WITHOUT merging/rewriting "
            "any rows (isolation for genuinely separate users is preserved). Set "
            "via HCC_IDENTITY_ALIASES as JSON, e.g. "
            '{"michael": ["michael", "ou_90cabb31bb5f47834ed31e603e44cd0c"]}.'
        ),
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
    qmd_min_importance: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "QMD knowledge-doc export threshold. A memory is distilled into a "
            "knowledge document when it is shared=true OR importance >= this "
            "value. Historically the generator required shared=true, but every "
            "OpenClaw/Hermes-synced memory is stored shared=false, so the KB "
            "produced 0 docs (2026-08-09 排查 P0-1). Gating on importance instead "
            "keeps raw low-value chatter out while still distilling the "
            "high-signal minority (~356 rows at 0.6)."
        ),
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

    # New-dims named-state thresholds (soul v0.2, 11 added dims) — checked
    # after the original 8-state cascade above (so an already-strong old
    # state like 依恋/雀跃 isn't clobbered by a milder new-dim signal),
    # in override-priority order (docs/09-soul模型化讨论.md), most
    # intense/least-ambiguous first. Single-threshold (not paired like the
    # old states) — same "not yet calibrated against real data" caveat.
    emotion_ecstasy_ecstasy: float = Field(default=0.5, ge=0.0, le=1.0)
    emotion_arousal_arousal: float = Field(default=0.5, ge=0.0, le=1.0)
    emotion_excitement_excitement: float = Field(default=0.45, ge=0.0, le=1.0)
    emotion_anger_anger: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_jealousy_jealousy: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_anxiety_anxiety: float = Field(default=0.4, ge=0.0, le=1.0)
    emotion_tenderness_tenderness: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_loneliness_loneliness: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_shyness_shyness: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_playfulness_playfulness: float = Field(default=0.3, ge=0.0, le=1.0)

    # Soul neural perception source (docs/09-soul模型化讨论.md) — text -> 17-dim
    # offsets from the trained soul_encoder, served over HTTP from umbrella
    # (tailscale). EmotionEngine.update_neural() tries this first and falls
    # back to the T3 keyword table (EMOTION_TRIGGERS/NEW_DIM_TRIGGERS) on any
    # failure — never a hard dependency, see core/emotion.py.
    soul_service_enabled: bool = Field(
        default=True,
        description="Master switch for the neural perception source (HCC_SOUL_SERVICE_ENABLED).",
    )
    soul_service_url: str = Field(
        default="http://127.0.0.1:8732",
        description="Base URL of the soul_encoder inference service on umbrella (HCC_SOUL_SERVICE_URL).",
    )
    soul_service_timeout: float = Field(
        default=2.0,
        description="Timeout in seconds for the soul service call (HCC_SOUL_SERVICE_TIMEOUT). "
        "On timeout/error, falls back to keyword triggers rather than blocking the caller.",
    )

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

    # --- Retrieval recency / source weighting (P2-7) --------------------
    # openclaw_sync bulk-migrated ~2000+ historical rows in one shot (same
    # RRF rank distribution as everything else), so they compete on equal
    # footing with genuinely new conversation memories at the same topical
    # relevance — old data drowns out new. This reweights hybrid_search's
    # already-fused rrf_score (multiplicatively, not a replacement — topical
    # relevance from BM25+vector stays the primary signal) by how old a
    # memory is and where it came from.
    retrieval_recency_weighting_enabled: bool = Field(
        default=True,
        description="Master switch for exponential recency decay applied to "
        "hybrid_search's fused rrf_score (HCC_RETRIEVAL_RECENCY_WEIGHTING_ENABLED).",
    )
    retrieval_recency_half_life_days: float = Field(
        default=60.0, gt=0,
        description="Half-life in days for the recency decay factor "
        "(HCC_RETRIEVAL_RECENCY_HALF_LIFE_DAYS) — a memory this old is "
        "weighted at 0.5x, twice this old at 0.25x, etc.",
    )
    retrieval_source_weights: dict[str, float] = Field(
        default_factory=lambda: {"openclaw_sync": 0.5},
        description="Per-Memory.source multiplier applied to rrf_score alongside "
        "recency decay (HCC_RETRIEVAL_SOURCE_WEIGHTS as JSON, e.g. "
        '\'{"openclaw_sync": 0.5}\'). Sources not listed default to 1.0 (no change).',
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
