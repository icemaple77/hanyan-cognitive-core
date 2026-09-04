"""Event-driven local-model noise review (docs/local-noise-filter.md 四).

Subscribes the process-wide :class:`~core.event_bus.EventBus` to
``MEMORY_CREATED`` so low-trust memory writes (OpenClaw's ``tool_result``
auto-log hook, currently ~300 rows and the single largest noise source per
docs/local-noise-filter.md 零) get an async second opinion from
``core.local_filter`` without adding latency to ``/memory/store`` — the review
runs after the HTTP response has already gone back to the caller.

Mirrors the subscription pattern in ``core.emotion_events``: connect once in
the gateway lifespan, one callback, all failures logged and swallowed (an
Ollama hiccup must never surface as a memory-API error, and never touches the
row that triggered it beyond what the review itself decides).

两种模式(2026-09-05,公子:「噪音过滤的 4b 模型还是要想办法减负」):

**batch(默认)**:MEMORY_CREATED 只是让行落库,**不调模型**;
积压的低信任行由 :func:`process_pending` 在 dreaming 时段一次性过完。
为什么改:一条一条实时判,意味着只要公子在跟 agent 聊天,ollama 就得把
4.7B 的 qwen3.5 拉进内存(4.8 GB)、判完还要留 5 分钟才卸载 —— 于是
「聊天」和「4B 常驻」几乎划等号。实测每天约 800 次调用、23 分钟推理。
批处理让模型**加载一次处理几百条**,而且发生在公子不用机器的时段。

**live**:老行为,逐条实时判。留着是因为它有一个批处理没有的性质 ——
噪音行在写入后几秒内就被降权/丢弃,不会在检索里露脸哪怕一小会儿。
真在意这一点就设 HCC_NOISE_FILTER_MODE=live。

**队列就是"没有 noise-filter 标签"这件事本身**,不额外维护一张表:
判过的行会被打上该标签,所以"低信任 且 无该标签"天然就是待办集合。
这样重启不丢队列,漏判的行下一轮自动补上,也不会重复判。
"""

from __future__ import annotations

import logging

from core.config import core_settings
from core.event_bus import Event, EventType
from core.local_filter import DISCARDED_STATUS, NOISE_FILTER_TAG, evaluate
from gateway.core.events import get_event_bus

logger = logging.getLogger(__name__)

# Sources of memory writes that bypass evaluate()-gated storage and are known
# to be dominated by noise (docs/local-noise-filter.md 零/六). Deliberately
# narrow: the much larger openclaw_memory/openclaw_sync bulk-import bucket is
# out of scope for P0 and needs a separate confirmation before it's included.
LOW_TRUST_TYPES = {"tool_result"}
LOW_TRUST_SOURCES = {"openclaw_plugin"}

# Low-trust rows are LOGS, not curated knowledge. Even a keep-verdict must never
# score them high enough to surface in search: a tool_result that quotes real
# memories (a search dump) reads as "informative" to the evaluator and used to
# get boosted to ~0.85, leaking into retrieval. 2026-08-26: 870 such rows had
# been inflated this way (then purged); cap keep-verdicts below the search-drop
# threshold (0.5) so a log can never be re-inflated to a surfacing score again.
LOW_TRUST_IMPORTANCE_CAP = 0.4


def _is_low_trust(payload: dict) -> bool:
    return payload.get("type") in LOW_TRUST_TYPES or payload.get("source") in LOW_TRUST_SOURCES


async def _mark_discarded(memory_id: str) -> None:
    from gateway.core.database import async_session
    from gateway.models import Memory

    async with async_session() as session:
        memory = await session.get(Memory, memory_id)
        if memory is None:
            return
        memory.status = DISCARDED_STATUS
        memory.tags = list({*(memory.tags or []), NOISE_FILTER_TAG})
        await session.commit()


async def _update_importance(memory_id: str, importance: float) -> None:
    from gateway.core.database import async_session
    from gateway.models import Memory

    async with async_session() as session:
        memory = await session.get(Memory, memory_id)
        if memory is None:
            return
        memory.importance = importance
        memory.tags = list({*(memory.tags or []), NOISE_FILTER_TAG})
        await session.commit()


async def _on_memory_created(event: Event) -> None:
    if not core_settings.noise_filter_enabled:
        return
    payload = event.payload
    if not _is_low_trust(payload):
        return
    if core_settings.noise_filter_mode != "live":
        # batch 模式:什么都不做。这一行没有 noise-filter 标签,
        # process_pending 下一轮自然会捞到它 —— 不需要入队动作。
        return

    memory_id = payload.get("memory_id")
    content = payload.get("content")
    if not memory_id or not content:
        return

    try:
        decision = await evaluate(str(content), memory_source=payload.get("source", ""))
    except Exception:
        logger.exception("noise_filter: evaluate raised for memory_id=%s", memory_id)
        return

    try:
        if decision.keep:
            capped = min(decision.importance, LOW_TRUST_IMPORTANCE_CAP)  # 低信任日志封顶,永不浮出检索
            await _update_importance(memory_id, capped)
            logger.info(
                "noise_filter: kept memory_id=%s importance=%s->%.2f (capped from %.2f, verdict=%s)",
                memory_id, payload.get("importance"), capped, decision.importance, decision.source,
            )
        else:
            await _mark_discarded(memory_id)
            logger.info(
                "noise_filter: discarded memory_id=%s importance=%.2f (verdict=%s)",
                memory_id, decision.importance, decision.source,
            )
    except Exception:
        logger.exception("noise_filter: DB update failed for memory_id=%s", memory_id)


async def process_pending(limit: int | None = None) -> dict[str, int]:
    """把积压的低信任行一次性过完。dreaming 深度阶段调用。

    待办集合 = 低信任 且 **没有** noise-filter 标签。不另建队列表:
      - 重启不丢队列(状态在库里)
      - 漏判的行下一轮自动补上
      - 判过的行带着标签,不会重复判(也就不会重复烧推理)

    并发用 noise_filter_concurrency(默认 4)。ollama 那侧
    OLLAMA_NUM_PARALLEL=1,再高也是排队,4 只是让请求不断流、
    别让 4.7B 的模型在两条之间空转等 HTTP 往返。
    """
    import asyncio

    from sqlalchemy import text as sql_text

    from gateway.core.database import async_session

    if not core_settings.noise_filter_enabled:
        return {"skipped": 1, "reason_disabled": 1}

    cap = limit or core_settings.noise_filter_batch_limit
    async with async_session() as session:
        rows = (await session.execute(sql_text(
            "select id, content, coalesce(source,'') from memories "
            "where (type = any(:types) or source = any(:sources)) "
            # 判过的都带这个标签 —— 它的缺席就是"待办"
            "  and cast(tags as text) not like :tag "
            "  and status = 'active' "
            "order by created_at limit :cap"
        ), {"types": list(LOW_TRUST_TYPES), "sources": list(LOW_TRUST_SOURCES),
            "tag": f"%{NOISE_FILTER_TAG}%", "cap": cap})).all()

    if not rows:
        logger.info("noise_filter batch: 没有积压")
        return {"pending": 0, "kept": 0, "discarded": 0, "failed": 0}

    logger.info("noise_filter batch: %d 条待判(上限 %d)", len(rows), cap)
    sem = asyncio.Semaphore(core_settings.noise_filter_concurrency)
    stats = {"pending": len(rows), "kept": 0, "discarded": 0, "failed": 0}

    async def one(memory_id: str, content: str, source: str) -> None:
        async with sem:
            try:
                decision = await evaluate(str(content), memory_source=source)
            except Exception:
                # 单条失败不打断整批。它没被打标签,下一轮还会被捞到 ——
                # 这正是"用标签缺席当队列"白得的重试语义。
                logger.exception("noise_filter batch: evaluate 失败 memory_id=%s", memory_id)
                stats["failed"] += 1
                return
        try:
            if decision.keep:
                await _update_importance(memory_id, min(decision.importance, LOW_TRUST_IMPORTANCE_CAP))
                stats["kept"] += 1
            else:
                await _mark_discarded(memory_id)
                stats["discarded"] += 1
        except Exception:
            logger.exception("noise_filter batch: 写回失败 memory_id=%s", memory_id)
            stats["failed"] += 1

    await asyncio.gather(*(one(r[0], r[1], r[2]) for r in rows))
    logger.info("noise_filter batch 完成: %s", stats)
    return stats


async def subscribe_noise_filter_events() -> None:
    """Subscribe the local noise filter to MEMORY_CREATED.

    Called once from the gateway lifespan on startup, alongside
    ``emotion_events.subscribe_emotion_events()``.
    """
    bus = get_event_bus()
    await bus.connect()
    await bus.subscribe([EventType.MEMORY_CREATED], _on_memory_created)
    logger.info(
        "noise_filter_events: subscribed to MEMORY_CREATED (enabled=%s, mode=%s, model=%s)",
        core_settings.noise_filter_enabled, core_settings.noise_filter_mode,
        core_settings.noise_filter_model,
    )
