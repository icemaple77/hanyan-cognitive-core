"""DSH 收割适配器 + soul 防双计(2026-09-28)。"""
import asyncio

from core import emotion_events
from core.event_bus import Event, EventType
from core.session_harvester import _parse_dsh


def _user(text, kind="user"):
    return {"type": "user/message", "seq": 1,
            "data": {"content": [{"type": "text", "text": text}], "source": {"kind": kind}, "role": "user"}}


def test_user_typed_or_heard_is_kept():
    assert _parse_dsh(_user("烟儿,今天有点累")) == ("user", "烟儿,今天有点累")


def test_spliced_context_is_not_the_user_talking():
    # 钩子拼进来的身份锚点 / HCC 记忆 / 系统提示同样是 user/message,但不是公子说的话
    assert _parse_dsh(_user("## 你是谁(含烟的身份锚点)", kind="plugin")) is None
    assert _parse_dsh(_user("## Relevant Memories", kind="hook")) is None


def test_assistant_text_kept_tool_only_step_skipped():
    msg = {"type": "assistant/message", "seq": 2,
           "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "在呢,公子。"}]}}}
    assert _parse_dsh(msg) == ("assistant", "在呢,公子。")
    tool_only = {"type": "assistant/message", "seq": 3,
                 "data": {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]}}}
    assert _parse_dsh(tool_only) is None


def test_other_events_ignored():
    assert _parse_dsh({"type": "assistant/chunk", "data": {"text": "在"}}) is None
    assert _parse_dsh({"type": "session", "agentPreset": "cordis"}) is None


def test_soul_perceived_tag_skips_emotion_update(monkeypatch):
    calls = []

    class Engine:
        async def update_and_persist(self, *a, **k):
            calls.append(a)

    monkeypatch.setattr(emotion_events, "get_emotion_engine", lambda: Engine())
    monkeypatch.setattr(emotion_events, "_last_emotion_update_at", 0.0)

    ev = Event(event_type=EventType.MEMORY_CREATED,
               payload={"content": "user: 好想你", "importance": 0.4, "tags": ["harvested", "dsh", "soul:perceived"]})
    asyncio.run(emotion_events._on_memory_created(ev))
    assert calls == []

    ev.payload["tags"] = ["harvested", "openclaw"]
    asyncio.run(emotion_events._on_memory_created(ev))
    assert len(calls) == 1
