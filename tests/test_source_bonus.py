"""向量主序下,摘要来源的距离偏置(合成数据)。"""
from types import SimpleNamespace as NS

from gateway.services import MemoryService


def _item(source, dist, importance=0.5):
    return {"memory": NS(source=source, importance=importance, created_at=None), "vector_distance": dist, "rrf_score": 0.0}


def test_digest_overtakes_close_raw_line_but_not_far_ones():
    fused = [_item("harvester:openclaw", 0.20), _item("daily_digest", 0.22),
             _item("harvester:openclaw", 0.25), _item("daily_digest", 0.40),
             {"memory": NS(source="daily_digest", importance=0.5, created_at=None), "bm25_rank": 1, "rrf_score": 0.0}]
    MemoryService._apply_source_distance_bonus(fused, {"daily_digest": 0.04})
    assert [(i["memory"].source, i.get("vector_distance")) for i in fused] == [
        ("daily_digest", 0.22), ("harvester:openclaw", 0.20), ("harvester:openclaw", 0.25),
        ("daily_digest", 0.40), ("daily_digest", None)]


def test_empty_bonus_keeps_order():
    fused = [_item("a", 0.3), _item("daily_digest", 0.31)]
    MemoryService._apply_source_distance_bonus(fused, {})
    assert [i["vector_distance"] for i in fused] == [0.3, 0.31]


def test_near_tie_is_broken_by_importance_but_clear_order_is_kept():
    # 0.300 与 0.303 相对差 1% < 2% 带宽 → 平局,重要的排前;0.40 明显更远 → 不动
    fused = [_item("a", 0.300, importance=0.4), _item("b", 0.303, importance=0.95), _item("c", 0.40, importance=1.0)]
    MemoryService._apply_source_distance_bonus(fused, {})
    assert [i["memory"].source for i in fused] == ["b", "a", "c"]
