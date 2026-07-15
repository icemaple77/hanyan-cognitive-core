# HCC ER 图 — 数据库实体关系

```mermaid
erDiagram
    User ||--o{ Memory : has
    User ||--o{ Preference : has
    User ||--o{ Personality : has
    
    Memory ||--o{ Embedding : has
    Memory ||--o{ EmotionSnapshot : has
    Memory ||--o{ KnowledgeLink : references
    
    KnowledgeLink ||--|| Knowledge : links
    
    GraphEntity ||--o{ GraphRelation : source
    GraphEntity ||--o{ GraphRelation : target
    
    DreamSession ||--o{ DreamSummary : produces
    DreamSession ||--o{ DreamReflection : produces
    
    EmotionState ||--o{ EmotionHistory : logs
    
    Memory {
        uuid id PK
        string user_id FK
        string type
        text content
        text summary
        float importance
        json tags
        string source
        string status
        vector embedding
        datetime created_at
        datetime updated_at
        int access_count
        float forget_score
    }
    
    GraphEntity {
        uuid id PK
        string name
        string type
        text summary
        float importance
        datetime created_at
    }
    
    GraphRelation {
        uuid id PK
        uuid source_id FK
        uuid target_id FK
        string relation_type
        float weight
        datetime created_at
    }
    
    EmotionState {
        uuid id PK
        string user_id FK
        float happiness
        float curiosity
        float fatigue
        float worry
        float closeness
        float focus
        datetime timestamp
    }
    
    Preference {
        string name PK
        string category
        float score
        int mention_count
        datetime first_seen
        datetime last_seen
        json examples
    }
    
    Knowledge {
        uuid id PK
        string title
        text content
        string topic
        float importance
        float confidence
        datetime created_at
    }
```

## 表清单

| 表名 | 说明 | 引擎 |
|------|------|:----:|
| memories | 长期记忆存储 | PostgreSQL + pgvector |
| graph_entities | 知识图谱节点 | PostgreSQL |
| graph_relations | 知识图谱边 | PostgreSQL |
| emotion_states | 情绪状态快照 | PostgreSQL |
| emotion_history | 情绪变化历史 | PostgreSQL |
| preferences | 用户偏好 | 内存（Redis 可选） |
| dream_sessions | 梦境会话记录 | PostgreSQL |
| knowledge | 知识条目 | PostgreSQL |
