"""Knowledge Graph: Entity-Relation models and query engine.

Stores entities (people, projects, concepts) and their relationships.
Uses PostgreSQL for persistence, with simple adjacency-based queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, String, Float, DateTime, Text, JSON, select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import Base


# ── Models ──────────────────────────────────────────────────────────────────

class Entity(Base):
    """A node in the knowledge graph (person, project, concept, etc.)."""

    __tablename__ = "graph_entities"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), index=True, nullable=False)
    type = Column(String(64), index=True, default="concept")  # person, project, concept, etc.
    summary = Column(Text, default="")
    metadata_json = Column(JSON, default=dict)
    importance = Column(Float, default=0.5)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
                        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class Relation(Base):
    """An edge connecting two entities."""

    __tablename__ = "graph_relations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(36), index=True, nullable=False)
    target_id = Column(String(36), index=True, nullable=False)
    relation_type = Column(String(64), index=True, default="related_to")  # loves, develops, runs, etc.
    weight = Column(Float, default=1.0)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


# ── Graph Engine ────────────────────────────────────────────────────────────

class GraphEngine:
    """Knowledge graph query engine."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_entity(self, name: str, type: str = "concept",
                         summary: str = "", importance: float = 0.5) -> Entity:
        entity = Entity(name=name, type=type, summary=summary, importance=importance)
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def add_relation(self, source_id: str, target_id: str,
                           relation_type: str = "related_to", weight: float = 1.0) -> Relation:
        relation = Relation(source_id=source_id, target_id=target_id,
                            relation_type=relation_type, weight=weight)
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def get_entity(self, entity_id: str) -> Entity | None:
        return await self.session.get(Entity, entity_id)

    async def find_entity(self, name: str) -> list[Entity]:
        stmt = select(Entity).where(Entity.name.ilike(f"%{name}%")).order_by(Entity.importance.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_relations(self, entity_id: str) -> list[dict[str, Any]]:
        """Get all relations for an entity, returning neighbor info."""
        stmt = select(Relation).where(
            (Relation.source_id == entity_id) | (Relation.target_id == entity_id)
        )
        result = await self.session.execute(stmt)
        relations = list(result.scalars().all())

        edges = []
        for rel in relations:
            neighbor_id = rel.target_id if rel.source_id == entity_id else rel.source_id
            neighbor = await self.session.get(Entity, neighbor_id)
            edges.append({
                "relation_id": rel.id,
                "relation_type": rel.relation_type,
                "weight": rel.weight,
                "neighbor": {"id": neighbor.id, "name": neighbor.name, "type": neighbor.type}
                if neighbor else None,
            })
        return edges

    async def query(self, entity_name: str, depth: int = 1) -> dict[str, Any]:
        """Query graph: find entity and its relations up to depth."""
        entities = await self.find_entity(entity_name)
        if not entities:
            return {"query": entity_name, "found": False}

        entity = entities[0]
        relations = await self.get_relations(entity.id)
        return {
            "query": entity_name,
            "found": True,
            "entity": {"id": entity.id, "name": entity.name, "type": entity.type, "summary": entity.summary},
            "relations": relations,
            "relation_count": len(relations),
        }
