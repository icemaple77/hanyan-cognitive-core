"""Memory Orchestrator — decides what to remember, what to discard.

Not every conversation gets saved. The orchestrator evaluates incoming content
and decides: keep? discard? merge with existing? How important is it?
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Content types that are low-value and should be discarded
LOW_VALUE_PATTERNS = [
    "吃了", "吃饭", "晚安", "早安", "好的", "嗯", "好的吧",
    "哈哈", "呵呵", "嘻嘻", "ok", "bye", "再见", "拜拜",
    "我上班", "下班了", "睡了", "起床", "去洗澡",
]

# Keywords that indicate high importance(技术/运维向,原为 OpenClaw 场景设计)
HIGH_IMPORTANCE_KEYWORDS = [
    "部署", "上线", "完成", "成功", "失败", "决定", "确认",
    "规划", "架构", "设计", "方案", "计划", "发布", "合并",
    "修复", "优化", "升级", "迁移", "重构", "新建",
    "项目", "配置", "连接", "地址", "密码", "token",
    "记得", "记住", "重要", "关键", "注意",
]

# 情感陪伴向关键词。HCC 现在也服务 hanyan(情感陪伴 agent),不能只有技术关键词——
# 否则"我想你了""今天很难过"这类对陪伴场景最重要的话会因不含任何关键词被判"不值得记"。
COMPANION_IMPORTANCE_KEYWORDS = [
    "想你", "爱你", "喜欢你", "离不开", "想我", "爱我",
    "难过", "开心", "委屈", "心疼", "生气", "吃醋", "孤独", "害怕",
    "纪念日", "生日", "约定", "承诺", "答应我", "永远",
    "谢谢你", "对不起", "抱抱", "陪我", "想哭", "崩溃了", "撑不住",
    "喜欢秋天", "喜欢的季节", "喜欢的颜色", "最喜欢",  # 偏好类事实
]

HIGH_IMPORTANCE_KEYWORDS = HIGH_IMPORTANCE_KEYWORDS + COMPANION_IMPORTANCE_KEYWORDS

# Topics that tend to be high-value
HIGH_VALUE_TOPICS = [
    "project", "deploy", "config", "architecture", "nas",
    "server", "docker", "database", "memory", "skill",
]


class OrchestratorDecision:
    """Result of the orchestrator's evaluation."""
    should_store: bool
    importance: float
    reason: str
    suggested_tags: list[str]
    suggested_summary: str | None


class MemoryOrchestrator:
    """Evaluates incoming content and decides whether and how to store it."""

    def __init__(self):
        self._recent_content: list[str] = []  # dedup against recent chatter

    def evaluate(self, content: str, source: str = "conversation",
                 user_id: str = "default") -> OrchestratorDecision:
        """Evaluate a piece of content and decide what to do with it."""
        decision = OrchestratorDecision()
        decision.should_store = True
        decision.importance = 0.3
        decision.reason = "default"
        decision.suggested_tags = []
        decision.suggested_summary = None

        content_lower = content.lower().strip()

        # 1. Check for low-value patterns
        if any(pattern in content_lower for pattern in LOW_VALUE_PATTERNS):
            # Only save if there's high-importance keyword too
            if not any(kw in content_lower for kw in HIGH_IMPORTANCE_KEYWORDS):
                decision.should_store = False
                decision.reason = "low_value_content"
                return decision

        # 2. Very short content → probably not worth saving
        if len(content) < 10 and not any(kw in content_lower for kw in HIGH_IMPORTANCE_KEYWORDS):
            decision.should_store = False
            decision.reason = "too_short"
            return decision

        # 3. Dedup against recent content
        for recent in self._recent_content[-5:]:
            if self._similar(content, recent) > 0.8:
                decision.should_store = False
                decision.reason = "duplicate_of_recent"
                return decision

        # 4. Calculate importance
        importance = 0.3
        keyword_matches = sum(1 for kw in HIGH_IMPORTANCE_KEYWORDS if kw in content_lower)
        importance += min(0.4, keyword_matches * 0.08)

        topic_matches = sum(1 for t in HIGH_VALUE_TOPICS if t in content_lower)
        importance += min(0.2, topic_matches * 0.05)

        # Longer content with substance → higher importance
        if len(content) > 100:
            importance += 0.1
        if len(content) > 300:
            importance += 0.1

        # Technical source → higher confidence
        if source in ("cli", "code", "api", "mcp"):
            importance += 0.1

        decision.importance = round(min(1.0, importance), 2)

        # 5. Generate tags from keywords
        matched_kw = [kw for kw in HIGH_IMPORTANCE_KEYWORDS if kw in content_lower]
        matched_topics = [t for t in HIGH_VALUE_TOPICS if t in content_lower]
        decision.suggested_tags = list(set(matched_kw[:3] + matched_topics[:2]))

        # 6. Generate reason
        if keyword_matches >= 3:
            decision.reason = "multiple_keywords_high_value"
        elif keyword_matches >= 1:
            decision.reason = f"keyword_match: {matched_kw[0]}"
        else:
            decision.reason = "general_content"

        # 7. Track for dedup
        self._recent_content.append(content)
        if len(self._recent_content) > 50:
            self._recent_content = self._recent_content[-30:]

        return decision

    def _similar(self, a: str, b: str) -> float:
        """Jaccard similarity on character bigrams.
        原实现按空格 split() 取词做 Jaccard —— 中文没有空格分词，整句会被当成一个
        "词"，导致中文相似度判断要么 0% 要么 100%，去重形同虚设。改用字符 2-gram，
        对中英文都适用（英文退化为词内重叠，仍然合理）。"""
        def bigrams(s: str) -> set[str]:
            s = s.lower().strip()
            return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}
        set_a, set_b = bigrams(a), bigrams(b)
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)


# Singleton
_orchestrator: MemoryOrchestrator | None = None


def get_orchestrator() -> MemoryOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = MemoryOrchestrator()
    return _orchestrator
