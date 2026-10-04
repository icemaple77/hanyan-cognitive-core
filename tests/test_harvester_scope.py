"""采集器只收含烟(main)的 OpenClaw 会话,不收专家 agent 的工作对话。"""
from core import session_harvester as sh


def test_openclaw_adapters_are_scoped_to_main():
    oc = [a for a in sh.ADAPTERS if a["name"] == "openclaw"]
    assert oc, "没有 openclaw 适配器"
    for a in oc:
        for key in ("glob", "jsonl_cutover"):
            if key in a:
                assert "/agents/main/" in a[key], f"{key} 不是限定 main: {a[key]}"
                assert "/agents/*/" not in a[key]
