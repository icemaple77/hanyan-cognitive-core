# HCC v2 Implementation Plan

Implement the following new modules in order:

## 1. redis_manager.py - Redis Working Memory + EventBus
- Redis client wrapper with TTL-based working memory
- EventBus using Redis Pub/Sub
- Config: HCC_REDIS_URL (default: redis://localhost:6379/0)
- Methods: set_working, get_working, publish_event, subscribe

## 2. event_bus.py - Event definitions
- MemoryCreated, MemoryUpdated, KnowledgeMerged, EmotionChanged, DreamFinished
- Typed event classes with metadata

## 3. qmd_generator.py - Knowledge Document Generator
- Reads from PostgreSQL, generates markdown files
- Generates: Knowledge/People/, Projects/, Timeline/, Rules/
- Config: HCC_QMD_DIR, HCC_QMD_GIT_ENABLED
- Auto git commit after generation

## 4. sync_engine.py - Bidirectional sync
- PostgreSQL → QMD (one-way generation)
- QMD → PostgreSQL (watch for changes, diff, merge)
- Handles conflicts gracefully

## 5. scanner upgrade - Add diff/merge/sync to existing scanner
- Add git-aware diff to scanner/watcher.py
- Add merge logic for QMD changes
- Connect scanner output to QMDGenerator

Requirements:
- All config via HCC_* env vars (Pydantic Settings)
- Redis: use redis-py async (redis.asyncio)
- QMD: atomic file writes, git integration optional
- EventBus: async pub/sub with proper cleanup
- Add redis, pyyaml deps to pyproject.toml
- Update docker-compose.yml for Redis
- Type hints, docstrings, error handling
