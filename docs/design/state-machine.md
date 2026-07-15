# HCC 记忆生命周期状态机

```mermaid
stateDiagram-v2
    [*] --> Candidate: 新内容产生
    Candidate --> Verified: Orchestrator 评估通过
    Candidate --> Discarded: 低价值内容
    
    Verified --> LongMemory: importance > 0.5
    Verified --> ShortMemory: importance 0.3-0.5
    
    ShortMemory --> LongMemory: 多次访问强化
    ShortMemory --> Archived: 长期未访问
    
    LongMemory --> Knowledge: Dream 引擎合并
    LongMemory --> Archived: Forget 引擎衰减
    
    Knowledge --> Archived: 知识老化
    
    Archived --> LongMemory: 再次被访问（Recall）
    Archived --> Forgotten: 遗忘分数 > 0.6
    
    Forgotten --> Archived: 重新激活
    Forgotten --> [*]: 永久删除
    
    note right of Verified: Orchestrator 决定<br/>importance + tags
    note right of LongMemory: Personality 引擎<br/>学习偏好
    note right of Knowledge: QMD 同步到<br/>Obsidian
```

## 状态说明

| 状态 | 说明 | 触发条件 |
|:-----|------|:---------|
| Candidate | 候选记忆，等待评估 | 新对话/文件产生 |
| Verified | 已确认有价值 | Orchestrator importance ≥ 0.3 |
| Discarded | 丢弃 | 低价值内容 |
| ShortMemory | 短期记忆 | importance 0.3-0.5 |
| LongMemory | 长期记忆 | importance ≥ 0.5 |
| Knowledge | 知识化 | Dream 引擎聚类合并 |
| Archived | 归档 | Forget 分数 > 0.6 |
| Forgotten | 遗忘 | Forget 分数 > 0.8 + 90天无访问 |
| | 永久删除 | Forget 分数 > 0.95 |
