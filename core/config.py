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
    # 只听本机。tailnet 上的客户端(aicore 的 openclaw)走
    # `tailscale serve --tcp 8000`,由 tailscaled 转发到这里 ——
    # 这样局域网/公网网卡上没有 8000,而 100.66.103.69:8000 照常可达,
    # openclaw.json 里的地址一个字都不用改(2026-09-04 收口)。
    api_host: str = Field(default="127.0.0.1", description="Gateway 监听地址。")
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
        # 默认值定成 soul,理由是**复发防线**而不是偏好:.env 一旦没被读到
        # (这个仓库真出过这事——见 gateway/core/embeddings.py 顶上那段
        # load_dotenv 注释),默认值就是实际生效值。默认成
        # sentence-transformers 就意味着"配置一丢,torch 悄悄回到进程里,
        # RSS 回到 2.6G",而且没有任何报错提示。默认成 soul 时同样的失误
        # 只会让嵌入调用**失败**(soul 没起就 raise → 记忆存 NULL、检索退 BM25),
        # 吵闹的失败比安静的内存膨胀好。
        default="soul",
        description="嵌入后端:soul | sentence-transformers | ollama | hash(仅测试)。"
        "**soul 是 2026-09-04 起的推荐值**:向 HanyanOS 的 soul 器官要向量,"
        "本进程不驻留 torch —— soul 本来就驻留着同一个 bge 骨干,两份纯浪费,"
        "而且 torch 批量 encode 的内存高水位不归还系统(实测 gateway 被顶到 2.4G)。",
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
    dream_rem_similarity: float = Field(
        default=0.75,
        description="REM 语义聚类的余弦阈值(HCC_DREAM_REM_SIMILARITY)。"
        "实测依据:窗口内 6352 条记忆两两余弦均值 0.517/p90 0.629/p99 0.722;"
        "0.75 时平均每条约 7 个邻居(0.70→28 个开始糊成团,0.80→2 个太紧)。"
        "调高=簇更纯更小,调低=簇更大更杂。",
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
    dream_knowledge_mode: str = Field(
        default="llm",
        description="Deep 阶段怎么产出知识(HCC_DREAM_KNOWLEDGE_MODE):\n"
        '"llm"(默认)= Deep 只挑出值得巩固的记忆组,记在本次运行的 stats.knowledge_groups 里,'
        "由每天早上的摘要阶段(scripts/daily_digest.py,umbrella 上的大模型)写成真正的知识;\n"
        '"template" = 老行为:当场用模板拼「综合自 N 条相关记忆的巩固摘要 + 原话摘录」。'
        "模板拼出来的不是摘要,只是把原话再抄一遍(2026-10-05 起弃用)。",
    )
    # 晋升前的降噪闸(2026-09-16):放宽 min_access_count 之后,深梦的"够格但没排上"
    # 名单里分数最高的两条是 cron 任务的系统提示(`[IMPORTANT: You are running as a
    # scheduled cron job...`,得分 1.05)。打分的四项(频次/标签数/新近/重要度)没有
    # 一项在量"这东西有没有价值",系统噪音恰好在频次和标签数上得分最高 —— 门槛一松,
    # 它们就会被晋升。晋升前让降噪模型再判一次,keep=false 的直接出局。
    # 成本:每晚至多 dream_limit 次调用,0.8b 每条约 0.47s。
    dream_promote_noise_check: bool = Field(
        default=True,
        description="Run the noise filter over Deep-phase promotion candidates and drop the ones "
        "it judges to be noise (HCC_DREAM_PROMOTE_NOISE_CHECK).",
    )
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
        default="http://127.0.0.1:9000",
        description="Base URL for reaching soul (HCC_SOUL_SERVICE_URL). "
        "2026-09-04 起指向 **HanyanOS core 的前门**,不再直连 soul:"
        "soul 改成监听 Unix socket(~/.hanyan/run/soul.sock)不再占端口,"
        "而 core 代理整棵 /soul/ 子树。这也是设计稿 §八 的原意——core 是唯一前门。"
        "路径不变(/soul/perceive、/soul/state、/soul/encode),只换基址。",
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

    # --- 写入侧预防（2026-09-29，见 gateway/core/write_guard.py）----------
    # 库里重复的来源是写入而非检索；这是入库前的拦截，默认关闭。
    store_dedupe_mode: str = Field(
        default="off",
        description="入库前精确查重（HCC_STORE_DEDUPE_MODE）：off(默认) | skip。"
        "skip = 窗口内已有逐字同文的 active 记忆时跳过插入，直接返回既有那条。"
        "不按 agent 分域——实测的重复恰是跨 agent 的同一段原文。",
    )
    store_dedupe_window_hours: int = Field(
        default=48,
        description="精确查重的回看窗口小时数（HCC_STORE_DEDUPE_WINDOW_HOURS）。"
        "限定窗口是为了让每次写入的查重查询有界；更老的重复交给 nightly 清扫。",
    )

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
    noise_filter_mode: str = Field(
        default="batch",
        description="噪音复核的时机:batch(默认)| live(HCC_NOISE_FILTER_MODE)。\n"
        "batch —— 写入时不调模型,积压的低信任行在 dreaming 时段一次性过完。\n"
        "live  —— 老行为,每条低信任写入实时判一次。\n"
        "2026-09-05 默认改成 batch(公子:「噪音过滤的 4b 模型还是要想办法减负」):"
        "逐条实时判意味着只要公子在跟 agent 聊天,ollama 就得把 4.7B 的 "
        "qwen3.5 拉进内存(4.8 GB),判完还留 5 分钟才卸载 —— 「聊天」和"
        "「4B 常驻」几乎划等号。实测每天约 800 次调用、23 分钟推理。"
        "批处理让模型加载一次处理几百条,而且发生在不用机器的时段。"
        "代价:噪音行会在库里多待几个小时才被降权 —— 但它们写入时 "
        "importance 就是 0.3,低于 0.5 的检索门槛,这几个小时里也浮不出来。",
    )
    noise_filter_batch_limit: int = Field(
        default=4000, ge=1,
        description="单轮 process_pending 最多处理多少条(HCC_NOISE_FILTER_BATCH_LIMIT)。"
        "2000 这个旧值是按 4b、每条 1.5s、每天约 800 条积压定的(2000 条约 12 分钟)。"
        "2026-09-16 两件事都变了:换成蒸馏的 0.8b 后每条 0.47s,而复核范围从"
        "tool_result 扩到 conversation/openclaw_memory/chatroom,日均积压涨到"
        "一两千条。4000 条约 31 分钟,仍在夜里跑得完,给积压留一倍追赶余量。",
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
    # 梦境日记:True 时先让本地模型(model_router 的 dream 档,现为 qwen3.5:4b)
    # 写一段,失败/超时/素材为空一律退回原来的模板渲染 —— 模板路径永远保留,
    # dreaming 绝不能因为模型没起来就写不出日记。
    dream_narrative_model_enabled: bool = Field(
        default=True,
        description="Let core/local_llm.py write the dream diary narrative, falling back to the "
        "template renderer on any failure (HCC_DREAM_NARRATIVE_MODEL_ENABLED).",
    )

    # core/local_llm.py 用(dreaming 写日记等)。比降噪的 15s 宽:降噪一条只吐
    # 一行 JSON,写日记要吐一整段,实测 qwen3.5:4b 约 8s,冷启动再加 3-4s。
    local_llm_timeout: float = Field(
        default=60.0, gt=0,
        description="Per-call HTTP timeout in seconds for core/local_llm.py text generation "
        "(HCC_LOCAL_LLM_TIMEOUT).",
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
    retrieval_importance_exponent: float = Field(
        default=0.5, ge=0.0, le=3.0,
        description="检索重排里 importance 的强度(HCC_RETRIEVAL_IMPORTANCE_EXPONENT)。"
        "phase3 起不再乘到 rrf_score 上,而是**只在近似平局区内**做 tie-break:"
        "平局区由 retrieval_importance_tiebreak_band 划定,区内按 importance 降序重排,"
        "区外主序完全不受影响。0 = 关闭(importance 完全不参与检索排序)。\n"
        "2026-09-05 加的,起因很直白:一条 importance 0.95 的策展知识"
        "(跨运行时变更总账)在真实查询里**排第 6**,压在它上面的是五条 "
        "importance 0.4、还带 stale 标签的 harvester 对话碎片。"
        "排序此前只看 recency + source,importance 完全不参与 —— "
        "于是「这条重要」这件事对检索毫无影响,策展知识被闲聊淹没。\n"
        "2026-09-28(phase3)修正:原来的**乘性**施加是错的 —— "
        "0.95^0.5 vs 0.4^0.5 差 5.6 倍,在 RRF 近似平局区里直接压过话题相关度,"
        "实测会把「查永久承诺」的紧急联络链顶到第 2。现降为 tie-break,"
        "主序仍由 BM25+向量的话题相关度决定。指数保留只为沿用那个 0.5 的调参语义。",
    )
    retrieval_importance_tiebreak_band: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="importance tie-break 的平局区宽度(HCC_RETRIEVAL_IMPORTANCE_TIEBREAK_BAND)。"
        "两条候选的 rrf_score 相对差 < 该值即视为平局,区内按 importance 降序重排;"
        "0 = 关闭 tie-break(等同只按 rrf_score)。默认 0.05,即相对差 5%。",
    )
    retrieval_fusion_mode: str = Field(
        default="rrf",
        description="检索融合模式(HCC_RETRIEVAL_FUSION_MODE)。\n"
        '"rrf"(默认)= BM25+向量 RRF 融合 + 乘性加权(现状);'
        '"vector_dominant"= 以**向量余弦序**为主序,BM25 仅作候选补充,乘性加权不参与主排序。\n'
        "2026-09-29 实测(可达子集 11 条 query 的 recall@5):纯向量 0.727 > RRF+加权 0.545 > "
        "再叠 rerank 0.273 —— 即现有后处理在把纯向量的好排序搞坏。",
    )
    retrieval_vector_tiebreak_band: float = Field(
        default=0.02, ge=0.0, le=1.0,
        description="vector_dominant 模式的平局区带宽(HCC_RETRIEVAL_VECTOR_TIEBREAK_BAND)。"
        "主序是向量余弦序;仅当相邻候选的距离相对差 < 该值时才视为平局,"
        "区内按 (importance, recency, source) 降序重排。即它们**不参与主排序**,只拆平局。"
        "0 = 关闭。默认 0.02:刻意收窄,不动清晰的向量顺序(实测纯向量序 recall@5 0.727 最优)。"
        "来自 fix/phase1-query-hygiene 分支(2026-09-29),2026-10-05 并入。",
    )
    retrieval_source_distance_bonus: dict[str, float] = Field(
        default_factory=lambda: {"daily_digest": 0.04},
        description="vector_dominant 模式下按 source 给余弦距离减去的偏置"
        "(HCC_RETRIEVAL_SOURCE_DISTANCE_BONUS,JSON)。默认让每日摘要略优先于原话;{} = 关闭。",
    )
    retrieval_user_scope: str = Field(
        default="strict",
        description="user_id 过滤口径(HCC_RETRIEVAL_USER_SCOPE)。\n"
        '"strict"(默认)= 按传入 user_id 过滤(现状);"all"= 不加 user_id 过滤。\n'
        "本系统是**单用户**;user_id 里混着微信路径 sessionID 与 system,"
        "实测把同一用户的 6/17 条期望记忆挡在检索外。放宽仅用于单用户部署。",
    )
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

    # --- Retrieval near-duplicate folding + diversity (phase4, 2026-09-28) --
    # 可达子集内 hybrid(0.545) 低于纯向量(0.727):RRF 只看名次,且 top-k 常被
    # 「同源近重复」(同一段对话被多个运行时/harvester 反复写入,向量几乎相同)
    # 整段占满 —— 5 个名额里 3~4 个是同一条内容的副本,真正的话题记忆排不上来。
    # 这一层在 RRF 融合 + recency/source 加权之后、最终截断之前,按**内容余弦**
    # (复用已存的 embedding,不额外调模型)把近重复折叠成一簇,只保留综合分最高
    # 的代表;被折叠的记入代表的 ``duplicates`` 字段(可观测、不丢弃)。折叠是
    # **内容驱动**的,不按 id / source 白名单硬编码。
    retrieval_dedup_enabled: bool = Field(
        default=True,
        description="近重复折叠总开关(HCC_RETRIEVAL_DEDUP_ENABLED)。关掉即为老行为。",
    )
    retrieval_duplicate_similarity_threshold: float = Field(
        default=0.95, ge=0.0, le=1.0,
        description="内容余弦 ≥ 该值即视为同一簇(HCC_RETRIEVAL_DUPLICATE_SIMILARITY_THRESHOLD)。"
        "默认 0.95:同源重复几乎完全共线(常 >0.99),而语义相近但不同的记忆通常 <0.93。"
        "设为 0(或把 retrieval_dedup_enabled 关掉)即关闭折叠。",
    )
    retrieval_diversity_enabled: bool = Field(
        default=True,
        description="折叠后仍不足 limit 时,用 MMR 在剩余候选里按相关度-新颖度权衡补齐"
        "(HCC_RETRIEVAL_DIVERSITY_ENABLED),避免同簇连续占位。",
    )
    retrieval_mmr_lambda: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="MMR 的 λ(HCC_RETRIEVAL_MMR_LAMBDA):λ*相关度 − (1−λ)*与已选的最大相似度。"
        "λ=1 等价于纯按分数;λ=0 只按新颖度。默认 0.7 偏相关度。",
    )
    retrieval_pool_completeness_guard: bool = Field(
        default=True,
        description="候选池完整性守卫(HCC_RETRIEVAL_POOL_COMPLETENESS_GUARD)。"
        "pgvector 的 HNSW 索引先按 ef_search 取全局近邻、**再**施加 user_id 等过滤,"
        "过滤尖时会**少返回**(实测 limit=50 只回 40~41 条,且精确名次第 3 的真实近邻"
        "被整条丢掉)—— 这就是「候选池截断」。打开后:一旦向量分支返回数 < 请求数,"
        "就对该查询改用精确扫描兜底(临时关索引扫描,查完还原),保证池子不缺。",
    )
    retrieval_pool_guard_ef_search_boost: bool = Field(
        default=True,
        description="池子守卫的首选轻量手段(HCC_RETRIEVAL_POOL_GUARD_EF_SEARCH_BOOST):"
        "先把 hnsw.ef_search 临时顶到上限再查(往往就够);仍不足才退回关索引的精确扫描。",
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
