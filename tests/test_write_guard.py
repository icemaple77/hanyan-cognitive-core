"""入库口对系统噪音的识别(合成数据)。"""
from gateway.core.write_guard import is_system_noise


def test_system_noise_prefixes_are_caught():
    assert is_system_noise("[OpenClaw session_end] session=abc")
    assert is_system_noise("user: [Inter-session message] sourceSession=agent:devops")
    assert is_system_noise("user: [Subagent Context] you are")
    assert is_system_noise("User: [IMPORTANT: Background process proc_1 completed")


def test_real_conversation_is_not_noise():
    assert not is_system_noise("user: 今天去海边吧")
    assert not is_system_noise("assistant: 找到线索:[OpenClaw session_end] 这类记录太多")
    assert not is_system_noise("")
    assert not is_system_noise(None)
