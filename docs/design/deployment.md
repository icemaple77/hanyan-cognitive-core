# HCC 部署图

## 单机版（Mac Mini M4）

```mermaid
graph TB
    subgraph "Mac Mini M4 (16GB)"
        H[Hermes Agent]
        HCC[Docker Compose]
        
        subgraph "Docker Containers"
            API[HCC API<br/>:8000]
            PG[(PostgreSQL<br/>+ pgvector<br/>:5433)]
            RD[(Redis<br/>:6381)]
        end
        
        QMD[QMD Knowledge<br/>./qmd/]
        AICore[(Obsidian<br/>AICore)]
    end
    
    H -->|HTTP| API
    API --> PG
    API --> RD
    API -->|sync| QMD
    QMD -->|watch| AICore
```

**适用场景：** 个人开发、测试、低负载使用
**模型：** ollama 本地模型（qwen3:8b / qwen3:14b）

## NAS 版（DS920+）

```mermaid
graph TB
    subgraph "N100 (Aicore)"
        HCC[Docker Compose]
        
        subgraph "Services"
            API[HCC API<br/>:8000]
            PG[(PostgreSQL<br/>+ pgvector)]
            RD[(Redis)]
        end
    end
    
    subgraph "DS920+ NAS"
        QMD[(QMD Knowledge<br/>Btrfs volume3)]
        BACKUP[(Backup<br/>volume1)]
    end
    
    subgraph "Mac Mini M4"
        H[Hermes]
        CC[Claude Code]
    end
    
    H -->|Tailscale| API
    CC -->|Tailscale| API
    API -->|sync| QMD
    QMD -->|daily backup| BACKUP
```

**适用场景：** 持续运行、数据持久化、知识管理
**模型：** N100 本地 ollama + Mac Mini M4 辅助计算

## 分布式版

```mermaid
graph TB
    subgraph "VPS (Sydney)"
        PROXY[nginx/frp<br/>vpn.chenjingtian.com]
    end
    
    subgraph "Mac Mini M4 (Brisbane)"
        H[Hermes + Agents]
        API_CLUSTER[HCC API<br/>read-replica]
    end
    
    subgraph "N100 (Shanghai)"
        HCC_MAIN[HCC API<br/>primary]
        PG_MAIN[(PostgreSQL<br/>primary)]
        RD[(Redis)]
        SCANNER[Scanner<br/>Watcher]
    end
    
    subgraph "DS920+ (Shanghai)"
        QMD[(QMD Knowledge)]
        SYNC[Sync Engine]
    end
    
    subgraph "SGE N100 (Shanghai)"
        TIAN["天天 Agent"]
    end
    
    PROXY -->|frp tunnel| HCC_MAIN
    H -->|Tailscale| HCC_MAIN
    TIAN -->|LAN 10.0.0.x| HCC_MAIN
    HCC_MAIN --> SYNC
    SYNC --> QMD
    API_CLUSTER -->|replication| PG_MAIN
```

**适用场景：** 生产环境、多 Agent、高可用
**模型：** 云端 GPT-4o / 本地 qwen3:14b 混合调度
