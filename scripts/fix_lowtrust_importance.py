#!/usr/bin/env python3
"""把泄出检索阈值的低信任行压回 noise_filter 的封顶线。

背景(2026-09-04):
  noise_filter 把低信任行(type=tool_result / source=openclaw_plugin)的 importance
  封顶在 LOW_TRUST_IMPORTANCE_CAP=0.4,为的是压在检索丢弃阈值 0.5 以下;
  它的注释还记着 2026-08-26 有 870 条被抬到 ~0.85 泄进检索、后来被清掉。

  而 dreaming 的 deep 阶段 `importance += 0.1` 把 0.4 顶成 0.5,正好越过阈值——
  等于每晚重造那批垃圾(deep 的洞已在 core/dream.py 修掉,这里清存量)。

**只压 importance,不删任何行。** 低信任行本身是有用的日志,只是不该浮进检索。
改动前把原值全量存盘,可回滚。
"""
import asyncio, json, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select

from core.noise_filter_events import LOW_TRUST_IMPORTANCE_CAP, LOW_TRUST_SOURCES, LOW_TRUST_TYPES
from gateway.core.database import async_session
from gateway.models import Memory

TAG = "importance-recapped:2026-09-04"


async def main(apply: bool) -> None:
    async with async_session() as s:
        rows = (await s.execute(
            select(Memory).where(Memory.status == "active")
        )).scalars().all()
        hits = [m for m in rows
                if (m.type in LOW_TRUST_TYPES or m.source in LOW_TRUST_SOURCES)
                and (m.importance or 0) > LOW_TRUST_IMPORTANCE_CAP]
        print(f"低信任且 importance>{LOW_TRUST_IMPORTANCE_CAP} 的行:{len(hits)}")
        snap = [{"id": str(m.id), "importance": m.importance, "type": m.type,
                 "source": m.source, "head": (m.content or "")[:70]} for m in hits]
        out = Path(__file__).parent / f"_lowtrust_before_{datetime.now(timezone.utc):%Y%m%d%H%M}.json"
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"原值已存盘 → {out.name}")
        for m in hits[:8]:
            print(f"  {m.importance:.2f} → {LOW_TRUST_IMPORTANCE_CAP}  {(m.content or '')[:56]}")
        if not apply:
            print("\n(dry-run;加 --apply 才真改)")
            return
        for m in hits:
            m.importance = LOW_TRUST_IMPORTANCE_CAP
            m.tags = list({*(m.tags or []), TAG})   # 留痕:哪些行被压过
        await s.commit()
        print(f"\n已压回 {len(hits)} 行,并打标 {TAG}")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
