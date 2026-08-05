"""Memory business logic service."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.fts import tokenize_for_fts
from gateway.core.rerank import RERANK_ENABLED, rerank as rerank_fn
from gateway.core.rrf import reciprocal_rank_fusion
from gateway.models import Memory
from gateway.schemas.memory import MemoryCreate, MemoryUpdate, MemorySearch

# OpenClaw's tool_result_persist hook auto-logs every tool call's raw output as a
# memory (importance=0.3 by default) — with thousands of these accumulated, they
# drown out real content in search results (recursive tool-log-of-a-tool-log
# quoting, near-zero relevance). exclude_noise (default on) drops them below this
# threshold unless the caller explicitly asks for type="tool_result"; anything
# manually promoted above the threshold stays searchable.
NOISE_TYPE = "tool_result"
NOISE_IMPORTANCE_THRESHOLD = 0.5


class MemoryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: MemoryCreate) -> Memory:
        memory = Memory(**data.model_dump())
        self.session.add(memory)
        await self.session.flush()
        return memory

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
        for key, value in update_data.items():
            setattr(memory, key, value)
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

    async def semantic_search(
        self,
        embedding: list[float],
        limit: int = 10,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        type: Optional[str] = None,
        exclude_noise: bool = True,
    ) -> list[tuple[Memory, float]]:
        """Return the ``limit`` memories most similar to ``embedding``.

        Similarity is measured with pgvector's cosine distance (0.0 == identical
        direction, 2.0 == opposite). Results are ordered nearest-first and each
        is returned alongside its raw cosine distance so callers can derive a
        similarity score. Memories without a stored embedding are excluded.
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
        return [(memory, float(dist)) for memory, dist in result.all()]

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
        """
        tokens = tokenize_for_fts(query)
        if not tokens:
            return []

        # plainto_tsquery (not websearch_to_tsquery): tokens are already
        # segmented by us, and plainto_tsquery has no special operator syntax
        # (quotes/OR/-) to misinterpret if a jieba token happens to start
        # with a character like '-'. It just ANDs every token together.
        tsvector_expr = func.to_tsvector("simple", Memory.search_text)
        tsquery_expr = func.plainto_tsquery("simple", tokens)
        rank = func.ts_rank_cd(tsvector_expr, tsquery_expr).label("rank")

        stmt = select(Memory, rank).where(
            tsvector_expr.op("@@")(tsquery_expr), Memory.status == "active"
        )
        if user_id:
            stmt = stmt.where(Memory.user_id == user_id)
        if agent_id:
            stmt = stmt.where(Memory.agent_id == agent_id)
        if type:
            stmt = stmt.where(Memory.type == type)
        stmt = self._apply_noise_filter(stmt, type, exclude_noise)
        stmt = stmt.order_by(rank.desc()).limit(limit)

        result = await self.session.execute(stmt)
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
        """
        bm25_results: list[tuple[Memory, float]] = []
        vector_results: list[tuple[Memory, float]] = []

        if query:
            bm25_results = await self.keyword_search_bm25(
                query, limit=candidate_pool, user_id=user_id, agent_id=agent_id, type=type
            )
        if embedding:
            vector_results = await self.semantic_search(
                embedding, limit=candidate_pool, user_id=user_id, agent_id=agent_id, type=type
            )

        fused = reciprocal_rank_fusion(bm25_results, vector_results)
        for item in fused:
            item["memory"] = item.pop("row")
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
