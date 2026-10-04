"""daily_digest 对模型输出形状的容错(合成数据,不碰任何真实对话)。"""
import datetime as dt
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "daily_digest", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "daily_digest.py"
)
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)

CH = [[{"id": "a", "line": "x", "t": dt.datetime.now()}, {"id": "b", "line": "y", "t": dt.datetime.now()}]]


def _run(monkeypatch, model_output):
    monkeypatch.setattr(dd, "ask_model", lambda prompt: model_output)
    stats = {"chunks": 0, "chunk_failed": 0, "episodes": 0, "preferences": 0, "facts": 0}
    return dd.digest_day("2026-10-04", CH, stats), stats


def test_object_items_keep_sources(monkeypatch):
    items, st = _run(monkeypatch, '{"episodes":[{"text":"今天换了硬盘","src":[0,1]}],"preferences":[],"facts":[]}')
    assert items == [{"date": "2026-10-04", "type": "episode", "text": "今天换了硬盘", "sources": ["a", "b"]}]
    assert st["episodes"] == 1 and st["chunk_failed"] == 0


def test_plain_string_items_accepted(monkeypatch):
    # 真实运行中模型偶尔把条目写成纯字符串,以前直接崩
    items, st = _run(monkeypatch, '{"episodes":["今天去了海边"],"preferences":["不喜欢香菜"],"facts":[]}')
    assert [(i["type"], i["text"], i["sources"]) for i in items] == [
        ("episode", "今天去了海边", []), ("preference", "不喜欢香菜", [])]
    assert st["episodes"] == 1 and st["preferences"] == 1


def test_malformed_pieces_skipped_not_crash(monkeypatch):
    items, st = _run(monkeypatch, '{"episodes":[null,5,{"text":""},{"text":"有效","src":"x"}],"preferences":"oops","facts":{"a":1}}')
    assert [i["text"] for i in items] == ["有效"] and items[0]["sources"] == []
    assert st["chunk_failed"] == 0


def test_non_json_counts_as_failed_chunk(monkeypatch):
    items, st = _run(monkeypatch, "抱歉我无法处理")
    assert items == [] and st["chunk_failed"] == 1
