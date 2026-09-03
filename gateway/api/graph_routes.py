"""Knowledge Graph API routes."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from core.graph import GraphEngine
from pydantic import BaseModel, Field

router = APIRouter()

_MERMAID_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HCC Knowledge Graph</title>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "dark" }});
</script>
<style>body {{ background: #1e1e2e; color: #cdd6f4; font-family: sans-serif; }}</style>
</head>
<body>
<pre class="mermaid">
{mermaid}
</pre>
</body>
</html>
"""


class EntityCreate(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = "concept"
    summary: str = ""
    importance: float = 0.5


class RelationCreate(BaseModel):
    source_id: str
    target_id: str
    relation_type: str = "related_to"
    weight: float = 1.0


class GraphQuery(BaseModel):
    entity_name: str
    depth: int = 1


@router.post("/graph/entity", summary="Add an entity")
async def add_entity(data: EntityCreate, session: AsyncSession = Depends(get_session)):
    engine = GraphEngine(session)
    entity = await engine.add_entity(data.name, data.type, data.summary, data.importance)
    return {"id": entity.id, "name": entity.name, "type": entity.type}


@router.post("/graph/relation", summary="Add a relation between entities")
async def add_relation(data: RelationCreate, session: AsyncSession = Depends(get_session)):
    engine = GraphEngine(session)
    relation = await engine.add_relation(data.source_id, data.target_id, data.relation_type, data.weight)
    return {"id": relation.id, "type": relation.relation_type}


@router.post("/graph/query", summary="Query the knowledge graph")
async def query_graph(data: GraphQuery, session: AsyncSession = Depends(get_session)):
    engine = GraphEngine(session)
    result = await engine.query(data.entity_name, data.depth)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"Entity '{data.entity_name}' not found")
    return result


@router.get("/graph/export", summary="Export the knowledge graph as Mermaid or a standalone HTML viewer")
async def export_graph(format: str = "mermaid", session: AsyncSession = Depends(get_session)):
    """体检报告 P1-5. ``format=mermaid`` (default) returns raw ``graph LR``
    text (paste into https://mermaid.live or any Mermaid-rendering client);
    ``format=html`` wraps it in a self-contained page that renders it via the
    Mermaid CDN script in-browser — no local Mermaid install needed."""
    if format not in ("mermaid", "html"):
        raise HTTPException(status_code=400, detail="format must be 'mermaid' or 'html'")
    engine = GraphEngine(session)
    mermaid = await engine.export_mermaid()
    if format == "html":
        return Response(content=_MERMAID_HTML_TEMPLATE.format(mermaid=mermaid), media_type="text/html")
    return Response(content=mermaid, media_type="text/plain")


@router.get("/graph/entity/{entity_id}", summary="Get entity details")
async def get_entity(entity_id: str, session: AsyncSession = Depends(get_session)):
    engine = GraphEngine(session)
    entity = await engine.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"id": entity.id, "name": entity.name, "type": entity.type, "summary": entity.summary}
