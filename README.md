# Hanyan Cognitive Core (HCC)
## Memory Operating System for AI Agents

A cognitive operating system that provides unified memory, emotion, dream, and knowledge services to AI agents.

## Architecture

See [AICore/含烟记忆系统/架构/HanyanMemoryCore-含烟记忆中心.md](../../AICore/含烟记忆系统/架构/HanyanMemoryCore-含烟记忆中心.md)

## Project Structure

```
HCC/
├── gateway/          — REST + MCP API Gateway
├── memory/           — Memory Engine
├── knowledge/        — Knowledge Graph Engine
├── emotion/          — Emotion Engine
├── dream/            — Dream Engine
├── personality/      — Personality Engine
├── planner/          — Planner Engine
├── scheduler/        — Task Scheduler
├── scanner/          — File Scanner
├── markdown/         — Markdown Sync
├── vector/           — Vector Manager
├── graph/            — Graph Manager
├── plugin/           — Plugin SDK
├── model-router/     — Model Router
├── pkg/              — Shared packages
├── docker/           — Docker configs
└── docs/             — Documentation
```

## Development Stages

| Stage | Module | Description |
|-------|--------|-------------|
| 1 | Gateway | REST + MCP interface |
| 2 | Memory | Store, search, forget |
| 3 | Scanner | File system scanning |
| 4 | Markdown Sync | DB ↔ Obsidian sync |
| 5 | Embedding | bge-m3 vectorization |
| 6 | Knowledge Graph | Entity-relation graph |
| 7 | Emotion | Emotion state system |
| 8 | Dream | Nightly reflection |
| 9 | Planner | Task planning |
| 10 | Plugin SDK | Hot-pluggable providers |
