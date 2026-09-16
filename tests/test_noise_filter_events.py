"""降噪复核的取舍规则测试。

为什么这几条值得测(2026-09-16 踩过):
扩大复核范围时,我先写成"keep 就把模型给的 importance 写回库",结果 233 条
对话/记忆行被顶到 0.8+,越过检索阈值 0.5 去跟真正重要的记忆抢位置。降噪的
职责是删噪音,不是重新定价 —— 这组测试把这条边界钉死。
"""

from __future__ import annotations

from core.noise_filter_events import (
    LOW_TRUST_IMPORTANCE_CAP,
    LOW_TRUST_SOURCES,
    LOW_TRUST_TYPES,
    REVIEW_TYPES,
    _is_low_trust,
    _keep_importance,
    _should_review,
)


class TestReviewScope:
    def test_low_trust_types_stay_a_subset_of_review_types(self):
        assert LOW_TRUST_TYPES <= REVIEW_TYPES

    def test_conversation_is_reviewed_but_not_low_trust(self):
        payload = {"type": "conversation", "source": "hermes"}
        assert _should_review(payload) is True
        assert _is_low_trust(payload) is False

    def test_tool_result_is_both(self):
        payload = {"type": "tool_result", "source": "openclaw_plugin"}
        assert _should_review(payload) is True
        assert _is_low_trust(payload) is True

    def test_curated_types_are_never_reviewed(self):
        # general/fact/knowledge 噪音占比低(16%/2%),knowledge 还是 dreaming
        # 自己的产出 —— 复核它等于复核做梦结果,要单独定策略。
        for mem_type in ("general", "fact", "knowledge"):
            assert _should_review({"type": mem_type, "source": "hermes"}) is False

    def test_plugin_source_is_reviewed_regardless_of_type(self):
        assert _should_review({"type": "whatever", "source": next(iter(LOW_TRUST_SOURCES))}) is True


class TestKeepImportance:
    def test_conversation_keep_never_repricies(self):
        # None = 不动 importance。返回 0.85 就是 2026-09-16 那个 bug。
        assert _keep_importance(0.85, low_trust=False) is None

    def test_log_keep_is_capped_below_search_threshold(self):
        capped = _keep_importance(0.85, low_trust=True)
        assert capped == LOW_TRUST_IMPORTANCE_CAP
        assert capped < 0.5  # gateway/services 的 NOISE_IMPORTANCE_THRESHOLD

    def test_log_keep_below_cap_is_left_alone(self):
        assert _keep_importance(0.2, low_trust=True) == 0.2
