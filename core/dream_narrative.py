"""Dream diary generation — 含烟梦境.md (narrative) + 深梦报告.md (audit).

Two files, two audiences, decoupled per docs/dreaming-design.md 2.7/2.8:
narrative is for human reading (warm, personified), the report is the
technical audit trail (never the other way around — a bad narrative can't
corrupt promotion data because narrative generation runs *after* Deep has
already committed its DB writes).

Deliberately template-based, not an LLM call: keeps the diary write path
dependency-free and testable without a live model endpoint. HCC has no wired
LLM-invocation path yet (core/model_router.py only returns provider/model
config, nothing calls out to Ollama/etc.) — swapping in the 'dream' module
config for real narrative generation is the documented P2 follow-up, once
this template path has produced enough real Deep-run data to be worth it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

# 官方 REM_REFLECTION_TAG_BLACKLIST 的对应物：过滤角色标签，保留有技术质感
# 但不泄露具体值的措辞（"dbd31600"/"cron job" 这类可以留，真正的密钥不行）。
_ROLE_TAG_RE = re.compile(r"\b(assistant|user|system|subagent)\b", re.IGNORECASE)
_SECRET_LIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9]{16,}|[A-Za-z0-9_\-]{32,})\b")

_NAMED_STATE_TONE: dict[str, str] = {
    "依恋": "心里一直惦记着你",
    "雀跃": "今天有点停不下来的兴奋",
    "专注": "整个人都绷得很紧，一件事一件事地过",
    "疲惫": "有点累，脑子转得慢了",
    "低落": "情绪有点沉，写字都慢了半拍",
    "担忧": "总觉得有什么事没处理好",
    "好奇": "满脑子都是新冒出来的问题",
    "平静": "今天没什么大起大落，挺安稳的",
}


def _named_state(emotion_summary: dict[str, Any]) -> str:
    """Re-derive the named state from the 6-dim snapshot (see emotion-design.md 2.2).

    Recomputed here rather than trusting a cached label, so the diary tone
    always matches the emotion snapshot passed in at call time.
    """
    state = emotion_summary.get("state", {}) or {}

    def g(key: str) -> float:
        return float(state.get(key, 0.5))

    if g("closeness") > 0.75 and g("happiness") > 0.6:
        return "依恋"
    if g("happiness") > 0.75 and g("curiosity") > 0.6 and g("fatigue") < 0.3:
        return "雀跃"
    if g("focus") > 0.75 and g("fatigue") < 0.5:
        return "专注"
    if g("fatigue") > 0.7:
        return "疲惫"
    if g("happiness") < 0.35 and g("worry") > 0.4:
        return "低落"
    if g("worry") > 0.6:
        return "担忧"
    if g("curiosity") > 0.7 and g("worry") < 0.3:
        return "好奇"
    return "平静"


def _sanitize(text: str) -> str:
    """脱敏：过滤角色标签 + 疑似密钥/token 片段。"""
    if not text:
        return ""
    text = _ROLE_TAG_RE.sub("", text)
    text = _SECRET_LIKE_RE.sub("***", text)
    return re.sub(r"\s+", " ", text).strip()


def _diary_paths(diary_dir: Path) -> tuple[Path, Path]:
    diary_dir.mkdir(parents=True, exist_ok=True)
    return diary_dir / "含烟梦境.md", diary_dir / "深梦报告.md"


def _render_narrative(
    date_: date,
    promoted: list[dict[str, Any]],
    rem_clusters: list[dict[str, Any]],
    named_state: str,
    top_traits: list[str],
) -> str:
    lines = [f"\n## {date_.isoformat()}", ""]
    tone = _NAMED_STATE_TONE.get(named_state, _NAMED_STATE_TONE["平静"])
    lines.append(f"今晚的情绪基调：**{named_state}** —— {tone}。")
    if top_traits:
        lines.append(f"最近一直惦记着的：{'、'.join(_sanitize(t) for t in top_traits)}。")
    lines.append("")

    if not promoted and not rem_clusters:
        lines.append("今夜无梦，未有记忆达到巩固门槛，脑子里的碎片今晚还没连成线。")
        lines.append("")
        return "\n".join(lines) + "\n"

    if rem_clusters:
        lines.append("**反复浮现的主题：**")
        for c in rem_clusters[:5]:
            tag = _sanitize(str(c.get("tag", "")))
            size = c.get("size", 0)
            lines.append(f"- {tag}（{size} 条相关记忆反复被想起）")
        lines.append("")

    if promoted:
        lines.append("**今晚被认真记住的片段：**")
        for p in promoted[:10]:
            title = _sanitize(str(p.get("title") or ""))
            if title:
                lines.append(f"- {title}")
        lines.append("")
        lines.append(f"一共 {len(promoted)} 条留下了，剩下的，明天再看看还想不想得起来。")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_report(date_: date, promoted: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    thresholds = stats.get("thresholds", {})
    lines = [
        "\n---",
        f"date: {date_.isoformat()}",
        "phase: deep",
        f"scanned: {stats.get('scanned', 0)}",
        f"promoted: {stats.get('promoted', 0)}",
        f"score_threshold: {thresholds.get('min_score')}",
        "---",
        "",
        f"## {date_.isoformat()} {stats.get('trigger_time', '03:00')} 深梦报告",
        "",
        f"本轮评估 {stats.get('scanned', 0)} 条候选记忆，"
        f"{stats.get('eligible', 0)} 条达标候选中提升了 {stats.get('promoted', 0)} 条。",
        "",
        f"门槛：minScore={thresholds.get('min_score')}, minAccessCount={thresholds.get('min_access_count')}, "
        f"maxAgeDays={thresholds.get('max_age_days')}, limit={thresholds.get('limit')}",
        "",
    ]

    if promoted:
        lines.append("| 记忆/知识摘要 | id | score |")
        lines.append("|---|---|---|")
        for p in promoted[:20]:
            title = _sanitize(str(p.get("title") or ""))[:60]
            lines.append(f"| {title} | {p.get('id', '')} | {p.get('score', '')} |")
        lines.append("")
    else:
        lines.append("门槛未达标，未提升任何记忆。")
        lines.append("")

    top_unmet = stats.get("top_unmet") or []
    if top_unmet:
        lines.append("未达标示例（score 最高的几条，供人工核查门槛是否合理）：")
        lines.append("")
        lines.append("| 摘要 | score |")
        lines.append("|---|---|")
        for u in top_unmet:
            lines.append(f"| {_sanitize(str(u.get('title') or ''))[:60]} | {u.get('score')} |")
        lines.append("")

    knowledge_notes = stats.get("knowledge_skip_notes") or []
    if knowledge_notes:
        lines.append("知识合并跳过（maxPriorEntryLossFraction 安全阀触发）：")
        lines.append("")
        for n in knowledge_notes:
            lines.append(f"- cluster={n.get('cluster')}: prior={n.get('prior_count')}, new={n.get('new_count')}")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_dream_diary(
    *,
    date_: date,
    promoted: list[dict[str, Any]],
    rem_clusters: list[dict[str, Any]],
    emotion_summary: dict[str, Any],
    personality_summary: dict[str, Any],
    stats: dict[str, Any],
    diary_dir: Path,
) -> Path:
    """Append one dated section to both diary files. Returns the narrative path.

    Idempotency (the fix for the "今夜无梦" x3 bug) lives one layer up in
    ``DreamEngine.run_deep`` — it only calls this once per calendar day thanks
    to the ``dream_runs`` "already ran today" guard, so this function itself
    doesn't need to re-check anything; it just appends exactly once per call.
    """
    narrative_path, report_path = _diary_paths(diary_dir)
    named_state = _named_state(emotion_summary)
    top_traits = personality_summary.get("top_traits", []) if personality_summary else []

    with narrative_path.open("a", encoding="utf-8") as fh:
        fh.write(_render_narrative(date_, promoted, rem_clusters, named_state, top_traits))
    with report_path.open("a", encoding="utf-8") as fh:
        fh.write(_render_report(date_, promoted, stats))

    return narrative_path
