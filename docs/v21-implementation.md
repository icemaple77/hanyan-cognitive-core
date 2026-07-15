# HCC v2.1 Implementation - Architecture Consolidation

## Phase 1: Provider SDK + Memory Provider

### 1. core/providers/base.py - Provider SDK
```python
class Provider(ABC):
    async def search(self, query: SearchQuery) -> SearchResult
    async def store(self, data: StoreData) -> StoreResult
    async def update(self, data: UpdateData) -> UpdateResult
    async def delete(self, id: str) -> bool
    async def health(self) -> HealthStatus
    async def metadata(self) -> ProviderMetadata
```

Dataclasses: SearchQuery, SearchResult, StoreData, StoreResult, UpdateData, HealthStatus, ProviderMetadata

### 2. core/providers/memory.py - Memory Provider (PostgreSQL)
Wraps existing gateway API calls into Provider SDK interface.
Reuses gateway/core/database.py and gateway/models.
Config: HCC_MEMORY_PROVIDER=postgresql (default)

### 3. core/providers/knowledge_qmd.py - Knowledge Provider (QMD)
Wraps existing core/qmd_generator.py into Provider SDK.
Config: HCC_KNOWLEDGE_PROVIDER=qmd (default)

### 4. core/managers/memory_manager.py - Memory Manager
Orchestrates Memory Provider calls.
Adds lifecycle, caching, fallback logic.

### 5. core/managers/knowledge_manager.py - Knowledge Manager
Orchestrates Knowledge Provider calls.
Search across multiple providers.

### 6. core/managers/context_builder.py - Context Builder
Assembles context from Memory + Knowledge + optional Emotion.
Output: structured context dict with sources.

### 7. core/query_planner.py - Query Planner
Analyzes query → determines which providers/managers to invoke.
Simple keyword/classification-based routing.

### 8. core/prompt_builder.py - Prompt Builder
Takes: Conversation + Memory + Knowledge + Emotion + Personality
Returns: assembled prompt string with metadata.

### 9. gateway/context.py - POST /context API
New FastAPI route: POST /api/v1/context
Input: {query, user_id, include_emotion?}
Output: {context, sources, provider_metadata}

### 10. gateway/api/context_routes.py - Context routes
Wires context API into existing gateway.

### Directory structure:
```
HCC/
├── core/
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py         - Provider SDK
│   │   ├── memory.py       - Memory Provider
│   │   └── knowledge_qmd.py - Knowledge Provider
│   ├── managers/
│   │   ├── __init__.py
│   │   ├── memory_manager.py
│   │   ├── knowledge_manager.py
│   │   └── context_builder.py
│   ├── query_planner.py
│   └── prompt_builder.py
└── gateway/
    ├── api/
    │   └── context_routes.py
    └── context.py
```

Do NOT delete or modify existing files unless necessary for refactoring.
Add new files. Keep backward compatibility.