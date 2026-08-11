"""Memory CRUD routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from core.memory_graph import build_memory_graph
from gateway.core.database import get_session
from gateway.core.events import publish_memory_event
from gateway.models import Memory
from gateway.schemas.memory import (
    MemoryCreate,
    MemoryUpdate,
    MemoryResponse,
    MemorySearch,
    MemoryListResponse,
    SemanticSearchRequest,
    HybridSearchRequest,
    HybridSearchItem,
    HybridSearchResponse,
)
from gateway.services import MemoryService

router = APIRouter()


@router.post("/memory/store", response_model=MemoryResponse)
async def store_memory(
    data: MemoryCreate,
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    service = MemoryService(session)
    memory = await service.create(data)
    await publish_memory_event(
        "store",
        memory.id,
        user_id=memory.user_id,
        content=memory.content,
        importance=memory.importance,
        tags=memory.tags,
        type=memory.type,
        source=memory.source,
    )
    return MemoryResponse.model_validate(memory)


@router.post("/memory/search", response_model=MemoryListResponse)
async def search_memories(
    query: MemorySearch,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    memories, total = await service.search(query)
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m in memories],
        total=total,
    )


@router.post("/memory/update", response_model=MemoryResponse)
async def update_memory(
    data: MemoryUpdate,
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    service = MemoryService(session)
    memory = await service.update(data)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    await publish_memory_event("update", memory.id, user_id=memory.user_id)
    return MemoryResponse.model_validate(memory)


@router.post("/memory/delete")
async def delete_memory(
    memory_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    service = MemoryService(session)
    success = await service.delete(memory_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    await publish_memory_event("delete", memory_id)
    return {"status": "ok", "message": "Memory deleted"}


@router.get("/memory/recent", response_model=MemoryListResponse)
async def recent_memories(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    memories, total = await service.get_recent(limit=limit, offset=offset)
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m in memories],
        total=total,
    )


class TouchRequest(BaseModel):
    ids: list[str]


@router.post("/memory/touch", summary="Recall reinforces memory (access_count+1, last_access=now)")
async def touch_memories(data: TouchRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """检索命中即"复述":access_count+1、last_access刷新 —— 遗忘引擎靠这个数据判断
    "常被谈起的事该记牢"。被检索到却从不 touch,forget_score 就只会随时间单调下降,
    等于遗忘机制形同虚设(半年前就是这个状态)。"""
    if not data.ids:
        return {"touched": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        sa_update(Memory)
        .where(Memory.id.in_(data.ids))
        .values(access_count=Memory.access_count + 1, last_access=now)
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"touched": result.rowcount}


@router.post("/memory/semantic-search", response_model=MemoryListResponse)
async def semantic_search_memories(
    query: SemanticSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    results = await service.semantic_search(
        embedding=query.embedding,
        limit=query.limit,
        user_id=query.user_id,
        agent_id=query.agent_id,
        type=query.type,
    )
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m, _ in results],
        total=len(results),
    )


@router.post("/memory/hybrid-search", response_model=HybridSearchResponse)
async def hybrid_search_memories(
    query: HybridSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> HybridSearchResponse:
    """BM25 + vector hybrid search (QMD-style), fused with RRF.

    Existing /memory/search (keyword ILIKE) and /memory/semantic-search
    (pure vector) endpoints are unchanged — this is additive, not a
    replacement, so existing clients keep working untouched.
    """
    service = MemoryService(session)
    results = await service.hybrid_search(
        query=query.query,
        embedding=query.embedding,
        limit=query.limit,
        user_id=query.user_id,
        agent_id=query.agent_id,
        type=query.type,
        candidate_pool=query.candidate_pool,
        rerank=query.rerank,
    )
    items = [
        HybridSearchItem(
            memory=MemoryResponse.model_validate(item["memory"]),
            rrf_score=item["rrf_score"],
            bm25_rank=item.get("bm25_rank"),
            bm25_score=item.get("bm25_score"),
            vector_rank=item.get("vector_rank"),
            vector_distance=item.get("vector_distance"),
            rerank_score=item.get("rerank_score"),
        )
        for item in results
    ]
    return HybridSearchResponse(items=items, total=len(items))


@router.get("/memory/graph", summary="Memory graph — nodes=memories, edges=semantic/temporal association")
async def memory_graph(
    user_id: str | None = None,
    agent_id: str | None = None,
    type: str | None = None,
    limit: int = 200,
    knn: int = 5,
    min_similarity: float = 0.5,
    temporal_window_hours: float = 6.0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """HanyanOS docs/14 三 记忆星空图 — see core.memory_graph for the full design
    note. Nodes are the ``limit`` most recent active memories (size=importance,
    color=keyword-derived dominant emotion); edges are pgvector cosine-similarity
    kNN pairs (``semantic``) plus same-type adjacent-in-time pairs (``temporal``).
    See ``GET /memory/graph/view`` for a self-contained d3 viewer of this data."""
    return await build_memory_graph(
        session,
        user_id=user_id,
        agent_id=agent_id,
        type=type,
        limit=limit,
        knn=knn,
        min_similarity=min_similarity,
        temporal_window_hours=temporal_window_hours,
    )


_MEMORY_GRAPH_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HCC 记忆星空图</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<style>
  body { margin: 0; background: #14141c; color: #cdd6f4; font-family: -apple-system, sans-serif; overflow: hidden; }
  #hud { position: fixed; top: 10px; left: 12px; font-size: 12px; opacity: 0.75; z-index: 10; }
  #tooltip {
    position: fixed; pointer-events: none; max-width: 320px; padding: 8px 10px;
    background: #1e1e2e; border: 1px solid #333; border-radius: 6px; font-size: 12px;
    line-height: 1.5; display: none; z-index: 20;
  }
  svg { display: block; }
  line { stroke-opacity: 0.35; }
  text.label { font-size: 9px; fill: #999; pointer-events: none; }
</style>
</head>
<body>
<div id="hud">HCC 记忆星空图 — 拖拽平移/滚轮缩放/悬停查看详情 — 节点大小=重要性 颜色=情绪 边=语义相似(实线)/时间相邻(虚线)</div>
<div id="tooltip"></div>
<svg></svg>
<script>
const params = new URLSearchParams(location.search);
const apiUrl = "/api/v1/memory/graph?" + params.toString();

fetch(apiUrl).then(r => r.json()).then(data => {
  const width = window.innerWidth, height = window.innerHeight;
  const svg = d3.select("svg").attr("width", width).attr("height", height);
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.1, 6]).on("zoom", e => g.attr("transform", e.transform)));

  const nodeCount = Math.max(data.nodes.length, 1);
  const sim = d3.forceSimulation(data.nodes)
    .force("link", d3.forceLink(data.edges).id(d => d.id)
      .distance(d => d.type === "semantic" ? 90 - d.weight * 40 : 60)
      .strength(d => d.type === "semantic" ? d.weight * 0.6 : 0.2))
    .force("charge", d3.forceManyBody().strength(-90 * Math.sqrt(nodeCount) / 8))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(d => 6 + d.importance * 14));

  const edge = g.append("g").selectAll("line").data(data.edges).join("line")
    .attr("stroke", d => d.type === "semantic" ? "#7C7C88" : "#4FB8AF")
    .attr("stroke-width", d => d.type === "semantic" ? Math.max(0.5, d.weight * 2.5) : 1)
    .attr("stroke-dasharray", d => d.type === "temporal" ? "3,3" : null);

  const node = g.append("g").selectAll("circle").data(data.nodes).join("circle")
    .attr("r", d => 5 + d.importance * 14)
    .attr("fill", d => d.emotion.color)
    .attr("fill-opacity", 0.85)
    .attr("stroke", "#0a0a10")
    .attr("stroke-width", 1)
    .style("cursor", "pointer")
    .call(d3.drag()
      .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));

  const label = g.append("g").selectAll("text").data(data.nodes).join("text")
    .attr("class", "label").attr("dy", d => 5 + d.importance * 14 + 10).attr("text-anchor", "middle")
    .text(d => d.label.length > 14 ? d.label.slice(0, 14) + "…" : d.label);

  const tooltip = d3.select("#tooltip");
  node.on("mouseenter", (e, d) => {
    tooltip.style("display", "block").html(
      "<b>" + d.type + "</b> · 重要性 " + d.importance.toFixed(2) +
      (d.emotion.dominant ? " · 情绪 " + d.emotion.dominant + " (" + d.emotion.valence + ")" : "") +
      "<br>" + d.preview + "<br><span style='opacity:.6'>" + (d.created_at || "") + "</span>"
    );
  }).on("mousemove", e => {
    tooltip.style("left", (e.clientX + 14) + "px").style("top", (e.clientY + 14) + "px");
  }).on("mouseleave", () => tooltip.style("display", "none"));

  sim.on("tick", () => {
    edge.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("cx", d => d.x).attr("cy", d => d.y);
    label.attr("x", d => d.x).attr("y", d => d.y);
  });
});
</script>
</body>
</html>
"""


@router.get("/memory/graph/view", summary="Self-contained d3 viewer for the memory graph (no build step, CDN d3)")
async def memory_graph_view() -> Response:
    """Same query params as ``/memory/graph`` (``?user_id=...&limit=...``) are
    forwarded client-side to the JSON endpoint — open e.g.
    ``/api/v1/memory/graph/view?limit=200`` directly in a browser."""
    return Response(content=_MEMORY_GRAPH_HTML, media_type="text/html")
