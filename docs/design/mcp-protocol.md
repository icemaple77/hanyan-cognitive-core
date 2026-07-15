# HCC MCP 通信协议图

```mermaid
graph TB
    subgraph "AI Agents"
        H[Hermes]
        OC[OpenClaw]
        CC[Claude Code]
        CX[Cursor]
        WU[Open WebUI]
    end
    
    subgraph "HCC Gateway"
        REST[REST API :8000]
        MCP[MCP Server stdio]
        WS[WebSocket]
    end
    
    subgraph "HCC Core"
        AUTH[Auth]
        ROUTER[Task Router]
        QP[Query Planner]
        PB[Prompt Builder]
        CTX[Context Builder]
    end
    
    subgraph "HCC Managers"
        MM[Memory Manager]
        KM[Knowledge Manager]
        EM[Emotion Engine]
        DM[Dream Engine]
        FM[Forget Engine]
        PM[Personality Engine]
        GM[Graph Engine]
        SM[Subconscious]
    end
    
    subgraph "HCC Providers"
        MP[Memory Provider]
        KP[Knowledge Provider]
        EP[Embedding Provider]
    end
    
    subgraph "Storage"
        PG[(PostgreSQL<br/>pgvector)]
        QMD[(QMD<br/>Obsidian)]
        RD[(Redis<br/>Optional)]
    end
    
    H -->|REST/MCP| REST
    OC -->|REST| REST
    CC -->|MCP| MCP
    CX -->|MCP| MCP
    WU -->|REST| WS
    
    REST --> AUTH
    MCP --> AUTH
    AUTH --> ROUTER
    
    ROUTER -->|memory| MM
    ROUTER -->|knowledge| KM
    ROUTER -->|emotion| EM
    ROUTER -->|context| CTX
    ROUTER -->|query| QP
    
    QP --> CTX
    CTX --> PB
    
    MM --> MP
    KM --> KP
    
    MP --> PG
    KP --> QMD
    EP -->|optional| RD
    
    EM --> PM
    FM --> MM
    DM --> MM
    GM --> PG
    SM --> MM
```

## 统一 API 接口

| 方法 | 端点 | Agent 调用 |
|:----|:-----|:-----------|
| REST | `POST /api/v1/context` | 单入口获取上下文 |
| REST | `POST /api/v1/memory/store` | 存储记忆 |
| REST | `POST /api/v1/memory/search` | 搜索记忆 |
| REST | `POST /api/v1/graph/query` | 查询知识图谱 |
| REST | `POST /api/v1/emotion/state` | 获取情绪状态 |
| MCP | `store_memory` | 通过 MCP 协议存储 |
| MCP | `search_memories` | 通过 MCP 协议搜索 |
| MCP | `semantic_search` | 语义搜索 |
| MCP | `get_recent_memories` | 最近记忆 |
| MCP | `delete_memory` | 删除记忆 |
