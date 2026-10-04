"""Memory business logic service."""

import asyncio
import logging
from datetime import datetime, timezone
from functools import reduce
from typing import Optional

from sqlalchemy import select, func, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import core_settings
from gateway.core.embeddings import EMBEDDING_MODEL, embed_text, memory_embedding_text
from gateway.core.events import publish_conflict_event
from gateway.core.folding import apply_folding_and_diversity
from gateway.core.fts import (
    bm25_query_tokens,
    BM25_MAX_OR_QUERY_TOKENS,
    BM25_MAX_QUERY_TOKENS,
)
from gateway.core.rerank import RERANK_ENABLED, rerank as rerank_fn
from gateway.core.rrf import reciprocal_rank_fusion
from gateway.core.write_guard import SYSTEM_NOISE_TAG, find_exact_duplicate, is_system_noise
from gateway.models import Memory, MemoryConflict
from gateway.schemas.memory import MemoryCreate, MemoryUpdate, MemorySearch

logger = logging.getLogger(__name__)

# OpenClaw's tool_result_persist hook auto-logs every tool call's raw output as a
# memory (importance=0.3 by default) — with thousands of these accumulated, they
# drown out real content in search results (recursive tool-log-of-a-tool-log
# quoting, near-zero relevance). exclude_noise (default on) drops them below this
# threshold unless the caller explicitly asks for type="tool_result"; anything
# manually promoted above the threshold stays searchable.
NOISE_TYPE = "tool_result"
NOISE_IMPORTANCE_THRESHOLD = 0.5

# Cosine distance below which a just-stored memory is considered "same topic"
# as an existing active one of the same type/user/agent scope (see
# _flag_stale_duplicates). Calibrated loosely — this is a lightweight staleness
# heuristic, not true contradiction detection (see that method's docstring).
STALE_DISTANCE_THRESHOLD = 0.25


class MemoryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: MemoryCreate) -> Memory:
        payload = data.model_dump()
        # Server always computes its own embedding now (unified vector space —
        # see gateway.core.embeddings / 体检报告 P0-1). A client-supplied vector
        # would silently mix a different model's space into the same pgvector
        # column and corrupt cosine-distance comparisons, so it's dropped here
        # rather than trusted; the field stays in the schema only so existing
        # callers that still pass it don't break.
        payload.pop("embedding", None)
        memory = Memory(**payload)
        if is_system_noise(memory.content):
            memory.status = "discarded"
            memory.tags = [*(memory.tags or []), SYSTEM_NOISE_TAG]

        # --- 写入侧预防（2026-09-29；默认关，见 core/config.py store_*）---
        # 先查重（省一次嵌入计算），再算向量。
        # tool_result 已在源头不落库（插件侧 tool_result_persist），所以这里不做截断
        # —— 源头关了就别再截断，两者是替代关系。
        duplicate = await find_exact_duplicate(
            self.session,
            content=memory.content or "",
            window_hours=core_settings.store_dedupe_window_hours,
        )
        if duplicate is not None:
            return duplicate

        text = memory_embedding_text(memory.content, memory.summary)
        try:
            memory.embedding = await asyncio.to_thread(embed_text, text)
            memory.embedding_model = EMBEDDING_MODEL
        except Exception:
            logger.exception("embed_text failed for new memory — storing without embedding")

        self.session.add(memory)
        await self.session.flush()

        if memory.embedding is not None and memory.status == "active":
            try:
                await self._flag_stale_duplicates(memory)
            except Exception:
                logger.exception("stale-duplicate flagging failed for memory %s", memory.id)

        return memory

    async def _flag_stale_duplicates(self, memory: Memory) -> None:
        """Tag older, highly-similar active memories of the same type/scope as ``stale``.

        This is the lightweight half of the 体检报告 P1-3 ask ("矛盾/陈旧检测").
        It is deliberately *not* contradiction detection — it has no notion of
        negation or fact conflict, only embedding-space proximity, so "北京是
        首都" and "北京不是首都" would both get flagged as the same topic. Real
        contradiction detection needs a judgment call ("does B conflict with
        A?"), which is a job for the existing local-model review pattern
        (core/noise_filter_events.py's async Ollama judge on MEMORY_CREATED)
        rather than a synchronous embedding-distance check on the write path —
        proposed as follow-up work, not implemented here.

        What this *does* do safely: when a new memory lands very close in
        embedding space to an older one (same type/user/agent, distance below
        ``STALE_DISTANCE_THRESHOLD``), the older memory gets a ``stale`` tag
        appended. Both stay retrievable — nothing is deleted or hidden — so a
        caller can filter/deprioritize ``stale`` results or just ignore the tag.

        Each flag is also recorded as a :class:`~gateway.models.MemoryConflict`
        row (durable, queryable audit trail — "what got superseded and when")
        and published as a ``MEMORY_CONFLICT`` event on the shared EventBus
        (real-time, same channel store/update/delete already use). Both are
        best-effort: a DB or Redis hiccup here must not fail the store request
        that triggered it.

        Scope is deliberately narrow to keep this a write-path check, not a
        full scan: at most 5 nearest neighbors, restricted to the same
        user/agent/type as the new memory (an existing ivfflat/hnsw-backed
        ANN query, not a table scan) — see ``semantic_search``.
        """
        candidates = await self.semantic_search(
            memory.embedding,
            limit=5,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            type=memory.type,
            exclude_noise=False,
        )
        for old, distance in candidates:
            if old.id == memory.id or distance > STALE_DISTANCE_THRESHOLD:
                continue
            if old.content.strip() == memory.content.strip():
                continue  # exact resubmission, not a superseding fact
            if "stale" not in (old.tags or []):
                old.tags = [*(old.tags or []), "stale"]
                self.session.add(MemoryConflict(
                    old_memory_id=old.id,
                    new_memory_id=memory.id,
                    distance=distance,
                    user_id=memory.user_id,
                    agent_id=memory.agent_id,
                    type=memory.type,
                ))
                await self.session.flush()
                await publish_conflict_event(
                    old.id, memory.id, distance,
                    user_id=memory.user_id, agent_id=memory.agent_id, type=memory.type,
                )

    async def search(self, query: MemorySearch) -> tuple[list[Memory], int]:
        stmt = select(Memory).where(Memory.status == "active")
        count_stmt = select(func.count(Memory.id)).where(Memory.status == "active")

        if query.user_id:
            stmt = stmt.where(Memory.user_id == query.user_id)
            count_stmt = count_stmt.where(Memory.user_id == query.user_id)
        if query.agent_id:
            stmt = stmt.where(Memory.agent_id == query.agent_id)
            count_stmt = count_stmt.where(Memory.agent_id == query.agent_id)
        if query.shared is not None:
            stmt = stmt.where(Memory.shared == query.shared)
            count_stmt = count_stmt.where(Memory.shared == query.shared)
        if query.type:
            stmt = stmt.where(Memory.type == query.type)
            count_stmt = count_stmt.where(Memory.type == query.type)
        if query.query:
            like = f"%{query.query}%"
            stmt = stmt.where(Memory.content.ilike(like) | Memory.summary.ilike(like))
            count_stmt = count_stmt.where(Memory.content.ilike(like) | Memory.summary.ilike(like))

        stmt = stmt.order_by(Memory.created_at.desc()).offset(query.offset).limit(query.limit)

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        return memories, total

    async def update(self, data: MemoryUpdate) -> Optional[Memory]:
        memory = await self.session.get(Memory, data.id)
        if memory is None:
            return None

        update_data = data.model_dump(exclude_unset=True, exclude={"id"})
        # 向量由服务端负责:客户端传来的维度不对就丢弃(老客户端发 1024 维,列是 768);
        # 正文/摘要变了而没带可用向量 → 这里重算,否则旧向量会和新内容对不上。
        client_vec = update_data.get("embedding")
        if client_vec is not None and len(client_vec) != core_settings.embedding_dim:
            logger.warning("update %s: client embedding has %d dims (server %d) — ignoring it",
                           data.id, len(client_vec), core_settings.embedding_dim)
            update_data.pop("embedding")
        text_changed = any(
            key in update_data and update_data[key] != getattr(memory, key)
            for key in ("content", "summary")
        )
        for key, value in update_data.items():
            setattr(memory, key, value)
        if text_changed and "embedding" not in update_data:
            try:
                memory.embedding = await asyncio.to_thread(
                    embed_text, memory_embedding_text(memory.content, memory.summary))
                memory.embedding_model = EMBEDDING_MODEL
            except Exception:
                logger.exception("update %s: embed_text failed — keeping the old embedding", data.id)
        memory.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)  # 列是naive datetime,
        # 之前这里直接赋值带时区的datetime,和Memory模型别处一致的写法不符,asyncpg会直接报错——
        # 说明 /memory/update 这条路径半年来大概率从没被真正调用过

        await self.session.flush()
        return memory

    async def delete(self, memory_id: str) -> bool:
        result = await self.session.execute(delete(Memory).where(Memory.id == memory_id))
        return result.rowcount > 0

    @staticmethod
    def _apply_noise_filter(stmt, type: Optional[str], exclude_noise: bool):
        """Drop low-importance tool_result rows, unless the caller explicitly asked for that type."""
        if exclude_noise and type != NOISE_TYPE:
            stmt = stmt.where(
                ~((Memory.type == NOISE_TYPE) & (Memory.importance < NOISE_IMPORTANCE_THRESHOLD))
            )
        return stmt

    @staticmethod
    def _apply_recency_source_weighting(fused: list[dict]) -> None:
        """Reweight RRF-fused results by memory age and source (P2-7), in place.

        Multiplies each item's ``rrf_score`` by a recency-decay factor
        (``0.5 ** (age_days / half_life)``, see ``retrieval_recency_half_life_days``)
        and a per-``Memory.source`` weight (``retrieval_source_weights``), then
        re-sorts. Multiplicative, not a replacement, so topical relevance from
        BM25+vector stays the dominant signal — this only nudges newer,
        non-bulk-migrated memories within a near-tie.

        ``importance`` is **not** multiplied in (phase3 fix): ``0.95 ** 0.5`` vs
        ``0.4 ** 0.5`` is a 5.6x gap, which swamped topical relevance inside
        RRF's near-tie band and (measured) lifted the emergency-contact chain to
        #2 on a "permanent commitment" query. It is now applied as a
        **tie-break only**: candidates whose ``rrf_score`` is within a relative
        ``retrieval_importance_tiebreak_band`` (default 5%) of the group's top
        are re-ordered by descending ``importance``; outside that band the
        primary order is untouched. ``retrieval_importance_exponent <= 0``
        disables the tie-break entirely.

        Recency and source stay multiplicative (unchanged defaults) rather than
        becoming tie-breaks too: their calibrated effects are small and
        monotone (60-day half-life; one 0.5x source weight), whereas importance's
        spread was the outlier that actually inverted rankings. Keeping the
        blast radius to the one broken signal.
        """
        if not fused:
            return
        exp = core_settings.retrieval_importance_exponent
        if (not core_settings.retrieval_recency_weighting_enabled
                and not core_settings.retrieval_source_weights and exp <= 0):
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        half_life = core_settings.retrieval_recency_half_life_days
        for item in fused:
            memory = item["memory"]
            weight = core_settings.retrieval_source_weights.get(memory.source, 1.0)
            if core_settings.retrieval_recency_weighting_enabled and memory.created_at:
                age_days = max(0.0, (now - memory.created_at).total_seconds() / 86400.0)
                weight *= 0.5 ** (age_days / half_life)
            item["rrf_score"] *= weight

        fused.sort(key=lambda item: item["rrf_score"], reverse=True)

        band = core_settings.retrieval_importance_tiebreak_band
        if exp <= 0 or band <= 0:
            return

        def _importance(item: dict) -> float:
            value = getattr(item["memory"], "importance", None)
            return 0.5 if value is None else float(value)

        # Greedy near-tie grouping against the group's top score, then a stable
        # re-order by importance inside each group. Compared to the group top
        # (not the previous item) so a slowly-decaying tail can't chain into one
        # giant "tie" spanning half the list.
        index = 0
        total = len(fused)
        while index < total:
            top = fused[index]["rrf_score"]
            denom = abs(top) if abs(top) > 1e-12 else 1e-12
            end = index + 1
            while end < total and (top - fused[end]["rrf_score"]) / denom < band:
                end += 1
            if end - index > 1:
                fused[index:end] = sorted(fused[index:end], key=_importance, reverse=True)
            index = end

    @staticmethod
    def _apply_source_distance_bonus(fused: list[dict], bonus: dict[str, float]) -> None:
        """向量主序下让提炼过的记忆(如每日摘要)略微优先于原话。

        同一件事,摘要条目和它出自的那几句原话向量很近;不加偏置时常是原话排前、
        摘要排后,注入块里就全是重复的旧对话。这里给指定 source 的行在余弦距离上
        减一个小量再重排——只动有向量距离的那一段,BM25 补进来的尾部不动。
        """
        if not bonus:
            return
        head = [it for it in fused if it.get("vector_distance") is not None]
        if len(head) < 2:
            return
        tail = [it for it in fused if it.get("vector_distance") is None]
        head.sort(key=lambda it: it["vector_distance"] - bonus.get(getattr(it["memory"], "source", None), 0.0))
        fused[:] = head + tail

    async def semantic_search(
        self,
        embedding: list[float],
        limit: int = 10,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        type: Optional[str] = None,
        exclude_noise: bool = True,
        ensure_complete: bool = False,
    ) -> list[tuple[Memory, float]]:
        """Return the ``limit`` memories most similar to ``embedding``.

        Similarity is measured with pgvector's cosine distance (0.0 == identical
        direction, 2.0 == opposite). Results are ordered nearest-first and each
        is returned alongside its raw cosine distance so callers can derive a
        similarity score. Memories without a stored embedding are excluded.

        ``ensure_complete`` (phase4): the HNSW index scans a bounded candidate
        set (``hnsw.ef_search``, default 40) *before* the ``user_id``/``type``
        filters are applied, so on a selective filter it can return **fewer
        than ``limit`` rows** and silently drop true near neighbours (measured:
        ``limit=50`` returned 41, and an exact-rank-3 neighbour was missing).
        When set and the first pass comes back short, retry with a boosted
        ``hnsw.ef_search`` and, if still short, with index scan disabled for
        that query (exact) — then restore the settings. Default off so existing
        callers (e.g. stale-duplicate flagging) are byte-for-byte unchanged;
        :meth:`hybrid_search` opts in via ``retrieval_pool_completeness_guard``.
        """
        distance = Memory.embedding.cosine_distance(embedding).label("distance")

        stmt = select(Memory, distance).where(
            Memory.embedding.is_not(None), Memory.status == "active"
        )
        if user_id:
            stmt = stmt.where(Memory.user_id == user_id)
        if agent_id:
            stmt = stmt.where(Memory.agent_id == agent_id)
        if type:
            stmt = stmt.where(Memory.type == type)
        stmt = self._apply_noise_filter(stmt, type, exclude_noise)
        stmt = stmt.order_by(distance).limit(limit)

        result = await self.session.execute(stmt)
        rows = result.all()
        if ensure_complete and limit and len(rows) < limit:
            rows = await self._complete_vector_pool(stmt, rows, limit)
        return [(memory, float(dist)) for memory, dist in rows]

    async def _complete_vector_pool(self, stmt, rows: list, limit: int) -> list:
        """Best-effort escalation when the ANN vector scan under-returns.

        Ordered cheapest-first: bump ``hnsw.ef_search`` to the pgvector ceiling
        (often enough — measured to recover the dropped neighbour), then, if
        still short, disable index scan for this one query so the planner does
        an exact scan. GUCs are set transaction-locally and restored afterwards;
        any failure falls back to the first-pass rows rather than erroring the
        search.
        """
        try:
            previous_ef = await self.session.scalar(
                text("select current_setting('hnsw.ef_search')")
            )
        except Exception:
            previous_ef = None
        try:
            if core_settings.retrieval_pool_guard_ef_search_boost:
                await self.session.execute(
                    text("select set_config('hnsw.ef_search', :v, true)"),
                    {"v": "1000"},
                )
                rows = (await self.session.execute(stmt)).all()
                if len(rows) >= limit:
                    return rows
            await self.session.execute(
                text("select set_config('enable_indexscan', 'off', true)")
            )
            try:
                rows = (await self.session.execute(stmt)).all()
            finally:
                await self.session.execute(
                    text("select set_config('enable_indexscan', 'on', true)")
                )
        except Exception:
            logger.exception(
                "vector pool completeness guard failed — keeping first-pass ANN result"
            )
        finally:
            if previous_ef is not None:
                try:
                    await self.session.execute(
                        text("select set_config('hnsw.ef_search', :v, true)"),
                        {"v": previous_ef},
                    )
                except Exception:
                    logger.exception("failed to restore hnsw.ef_search")
        return rows

    async def keyword_search_bm25(
        self,
        query: str,
        limit: int = 20,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        type: Optional[str] = None,
        exclude_noise: bool = True,
    ) -> list[tuple[Memory, float]]:
        """BM25-style full-text search via Postgres ``ts_rank_cd``.

        ``query`` is tokenized the same way ``search_text`` was at write time
        (see :mod:`gateway.core.fts`) so both sides of ``@@`` line up for
        mixed zh/en content. Returns memories ordered by descending rank
        alongside their raw rank score. Empty/unmatched queries return ``[]``
        rather than falling back to a full scan.

        Two tiers (phase3 fix):

        1. **AND** — ``plainto_tsquery`` over the capped token string. Precise,
           and the only tier used in the normal case.
        2. **OR fallback** — only if tier 1 returns zero rows. A long query
           (chat message + pasted context/injection block) ANDs dozens of
           tokens together, so almost no row satisfies *all* of them and the
           BM25 branch went silently empty, degrading hybrid_search to
           pure-vector. Tier 2 ORs the same tokens, still under the same
           ``status='active'`` / ``exclude_noise`` filters and the same
           ``ORDER BY ts_rank_cd DESC`` — so recall comes back and relevance
           ordering is still carried by the rank, not by OR hitting more rows.
           When both tiers match, AND wins (higher precision, no dilution).

        The OR tsquery is built as a chain of ``plainto_tsquery('simple', tok)
        || ...`` rather than by joining tokens into a ``to_tsquery`` string:
        ``plainto_tsquery`` treats its whole input as plain text (no operator
        syntax to escape), so a jieba token containing ``&``/``|``/``!``/``:``
        can't be misread as an operator, and no hand-rolled escaping is needed.
        The chain depth equals the token count, which
        ``BM25_MAX_OR_QUERY_TOKENS`` bounds.
        """
        token_list = bm25_query_tokens(query, BM25_MAX_QUERY_TOKENS)
        if not token_list:
            return []

        def _build_stmt(tsquery_expr):
            # 用触发器维护的 search_tsv 列,而不是现算 to_tsvector(search_text):
            # 后者让 PG 为每个命中行重解析全文,ORDER BY rank 更逼它全算一遍
            # (实测 507ms → 11.8ms,43x)。列由 trg_memories_search_tsv 保证同步。
            rank = func.ts_rank_cd(Memory.search_tsv, tsquery_expr).label("rank")
            stmt = select(Memory, rank).where(
                Memory.search_tsv.op("@@")(tsquery_expr), Memory.status == "active"
            )
            if user_id:
                stmt = stmt.where(Memory.user_id == user_id)
            if agent_id:
                stmt = stmt.where(Memory.agent_id == agent_id)
            if type:
                stmt = stmt.where(Memory.type == type)
            stmt = self._apply_noise_filter(stmt, type, exclude_noise)
            return stmt.order_by(rank.desc()).limit(limit)

        # Tier 1: AND. plainto_tsquery (not websearch_to_tsquery): tokens are
        # already segmented by us, and plainto_tsquery has no special operator
        # syntax (quotes/OR/-) to misinterpret if a jieba token happens to start
        # with a character like '-'. It just ANDs every token together.
        and_query = func.plainto_tsquery("simple", " ".join(token_list))
        result = await self.session.execute(_build_stmt(and_query))
        rows = result.all()
        if rows:
            return [(memory, float(r)) for memory, r in rows]

        # Tier 2: OR fallback. A single token makes OR identical to AND (already
        # known empty), and more OR terms than the depth cap would only add
        # noise, so bail out instead of re-running.
        or_tokens = token_list[:BM25_MAX_OR_QUERY_TOKENS]
        if len(or_tokens) < 2:
            return []
        or_query = reduce(
            lambda left, right: left.op("||")(right),
            (func.plainto_tsquery("simple", token) for token in or_tokens),
        )
        result = await self.session.execute(_build_stmt(or_query))
        return [(memory, float(r)) for memory, r in result.all()]

    async def hybrid_search(
        self,
        query: str = "",
        embedding: Optional[list[float]] = None,
        limit: int = 10,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        type: Optional[str] = None,
        candidate_pool: int = 50,
        rerank: bool = False,
    ) -> list[dict]:
        """BM25 + vector hybrid search, fused with Reciprocal Rank Fusion.

        Either ``query`` or ``embedding`` (or both) may be supplied — a
        branch is simply skipped if its input is missing, so this degrades
        gracefully to pure-vector or pure-BM25 search rather than erroring.
        ``candidate_pool`` controls how many results each branch contributes
        before fusion (wider than ``limit`` so RRF has enough to work with).
        ``rerank=True`` runs the fused top-``candidate_pool`` through the
        optional cross-encoder (see :mod:`gateway.core.rerank`) — but only if
        the server also has ``HCC_RERANK_ENABLED=true``; that env var is the
        ops-level kill switch (don't load a 600MB model / spend the per-query
        latency unless the deployment opted in), while this parameter is the
        per-request ask. Either one off means RRF order is kept as-is; if the
        reranker is enabled but fails to load/score, that also silently falls
        back to RRF order rather than erroring the request.

        Returns a list of dicts (not ORM tuples) since each result carries
        provenance beyond the plain :class:`Memory` row: ``memory``,
        ``rrf_score``, ``bm25_rank``/``bm25_score``, ``vector_rank``/
        ``vector_distance``, and (if reranked) ``rerank_score``.

        If ``query`` is given but ``embedding`` is not, the query text is
        embedded server-side (体检报告 P0-1 — callers, including the MCP tools
        and the OpenClaw plugin, no longer need their own embedding model to
        get a real vector branch; they just pass text).
        """
        bm25_results: list[tuple[Memory, float]] = []
        vector_results: list[tuple[Memory, float]] = []

        if embedding and len(embedding) != core_settings.embedding_dim:
            logger.warning("hybrid_search: client embedding has %d dims (server %d) — ignoring it",
                           len(embedding), core_settings.embedding_dim)
            embedding = None
        if query and not embedding:
            try:
                embedding = await asyncio.to_thread(embed_text, query, is_query=True)
            except Exception:
                logger.exception("embed_text failed for hybrid_search query — falling back to BM25-only")

        # 阶段5(2026-09-29): 单用户系统下可放宽 user_id 过滤 ——
        # 实测同一用户（微信 sessionID / system 名下）的 6/17 条期望记忆被它挡在检索外。
        scope_user_id = None if core_settings.retrieval_user_scope == "all" else user_id
        if query:
            bm25_results = await self.keyword_search_bm25(
                query, limit=candidate_pool, user_id=scope_user_id, agent_id=agent_id, type=type
            )
        if embedding:
            vector_results = await self.semantic_search(
                embedding,
                limit=candidate_pool,
                user_id=scope_user_id,
                agent_id=agent_id,
                type=type,
                ensure_complete=core_settings.retrieval_pool_completeness_guard,
            )

        # 阶段5(2026-09-29): 融合模式。实测纯向量序(0.727)优于 RRF+乘性加权(0.545),
        # 故提供 vector_dominant —— 向量余弦为主序,BM25 只补候选,乘性加权不参与主排序。
        if core_settings.retrieval_fusion_mode == "vector_dominant":
            fused = []
            _seen = set()
            for rank, (row, distance) in enumerate(vector_results, start=1):
                fused.append(
                    {"row": row, "vector_rank": rank, "vector_distance": distance, "rrf_score": 0.0}
                )
                _seen.add(row.id)
            for rank, (row, score) in enumerate(bm25_results, start=1):
                if row.id in _seen:
                    continue
                fused.append(
                    {"row": row, "bm25_rank": rank, "bm25_score": score, "rrf_score": 0.0}
                )
            for item in fused:
                item["memory"] = item.pop("row")
            self._apply_source_distance_bonus(fused, core_settings.retrieval_source_distance_bonus)
        else:
            fused = reciprocal_rank_fusion(bm25_results, vector_results)
            for item in fused:
                item["memory"] = item.pop("row")
            self._apply_recency_source_weighting(fused)

        # phase4: fold near-duplicate clusters (by stored-embedding cosine) out
        # of the ranked list, then MMR-backfill if folding left us short of
        # ``limit``. Content-driven only — no id/source allow-lists. Runs after
        # recency/source weighting so "best" means the final composite score,
        # and before the truncation/rerank below so the freed slots are real.
        fused = apply_folding_and_diversity(
            fused,
            limit=limit,
            enabled=core_settings.retrieval_dedup_enabled,
            threshold=core_settings.retrieval_duplicate_similarity_threshold,
            diversity_enabled=core_settings.retrieval_diversity_enabled,
            mmr_lambda=core_settings.retrieval_mmr_lambda,
        )
        do_rerank = rerank and RERANK_ENABLED
        top = fused[: max(limit, candidate_pool) if do_rerank else limit]

        if do_rerank and top:
            scores = await rerank_fn(query or "", [item["memory"].content for item in top])
            if scores is not None:
                for item, score in zip(top, scores):
                    item["rerank_score"] = score
                top.sort(key=lambda item: item["rerank_score"], reverse=True)

        return top[:limit]

    async def get_recent(self, limit: int = 20, offset: int = 0) -> tuple[list[Memory], int]:
        stmt = (
            select(Memory)
            .where(Memory.status == "active")
            .order_by(Memory.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = select(func.count(Memory.id)).where(Memory.status == "active")

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        return memories, total
