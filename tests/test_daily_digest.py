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


def test_non_json_chunk_is_retried_once(monkeypatch):
    outs = iter(["抱歉我无法处理", '{"episodes":["补跑成功"],"preferences":[],"facts":[]}'])
    monkeypatch.setattr(dd, "ask_model", lambda prompt: next(outs))
    stats = {"chunks": 0, "chunk_failed": 0, "episodes": 0, "preferences": 0, "facts": 0}
    items = dd.digest_day("2026-10-04", CH, stats)
    assert [i["text"] for i in items] == ["补跑成功"]
    assert stats["chunk_failed"] == 0 and stats["chunk_retried"] == 1


def test_failed_chunk_is_recorded_without_content(monkeypatch):
    _, st = _run(monkeypatch, None)
    assert st["chunk_failed"] == 1 and st["failed_chunks"] == [{"date": "2026-10-04", "chunk": 0}]


def test_write_items_skips_duplicates_and_tags_rows(monkeypatch):
    stored = []

    def fake_post(path, body):
        if path == "/memory/hybrid-search":
            dup = body["query"] == "公子不吃香菜"
            return {"items": [{"memory": {"type": "preference", "source": "daily_digest"},
                               "vector_distance": 0.02 if dup else 0.5}]}
        stored.append(body)
        return {"id": "x"}

    monkeypatch.setattr(dd, "_post", fake_post)
    stats = {}
    dd.write_items([
        {"date": "2026-10-03", "type": "preference", "text": "公子不吃香菜", "sources": []},
        {"date": "2026-10-03", "type": "episode", "text": "周六去了海边散步", "sources": []},
        {"date": "2026-10-03", "type": "fact", "text": "短", "sources": []},
    ], stats)
    assert stats == {"written": 1, "skipped_dup": 1, "write_failed": 0}
    assert stored[0]["type"] == "event" and stored[0]["source"] == "daily_digest"
    assert "digest:2026-10-03" in stored[0]["tags"] and "soul:perceived" in stored[0]["tags"]


def test_near_raw_conversation_is_not_a_duplicate(monkeypatch):
    monkeypatch.setattr(dd, "_post", lambda path, body: {"items": [
        {"memory": {"type": "conversation", "source": "harvester:openclaw"}, "vector_distance": 0.01}]})
    assert dd._is_duplicate("公子不吃香菜") is False


def test_knowledge_stage_writes_summary_and_discards_empty(monkeypatch):
    posted = []
    monkeypatch.setattr(dd, "_get", lambda path: {"total_pending": 3, "groups": [
        {"key": "rem-a", "existing_id": "k1", "members": [{"id": "m1", "text": "x"}, {"id": "m2", "text": "y"}]},
        {"key": "rem-b", "existing_id": "k2", "members": [{"id": "m3", "text": "x"}, {"id": "m4", "text": "y"}]},
        {"key": "legacy-k3", "existing_id": "k3", "members": []},
    ]})
    outs = iter(['{"title":"NAS 换盘","points":["公子把 NAS 的硬盘换了","数据次日迁移"]}', '{"title":"","points":[]}'])
    monkeypatch.setattr(dd, "ask_model", lambda prompt: next(outs))

    def fake_post(path, body):
        posted.append(body)
        return {"action": "updated" if body.get("content") else "discarded"}

    monkeypatch.setattr(dd, "_post", fake_post)
    stats = {}
    dd.summarize_knowledge(30, stats)
    assert stats["knowledge_groups"] == 3 and stats["knowledge_written"] == 1 and stats["knowledge_empty"] == 2
    assert posted[0]["title"] == "NAS 换盘" and posted[0]["content"].startswith("- 公子把 NAS")
    assert "content" not in posted[1] and "content" not in posted[2]


def test_knowledge_stage_leaves_group_alone_when_model_gives_no_json(monkeypatch):
    posted = []
    monkeypatch.setattr(dd, "_get", lambda path: {"groups": [
        {"key": "rem-a", "existing_id": "k1", "members": [{"id": "m1", "text": "x"}, {"id": "m2", "text": "y"}]}]})
    monkeypatch.setattr(dd, "ask_model", lambda prompt: "抱歉")
    monkeypatch.setattr(dd, "_post", lambda path, body: posted.append(body) or {})
    stats = {}
    dd.summarize_knowledge(30, stats)
    assert posted == [] and stats["knowledge_failed"] == 1
