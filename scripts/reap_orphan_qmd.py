#!/usr/bin/env python3
"""回收 Obsidian 里的孤儿 md —— 记忆已 discarded/不存在,文件还留着。

为什么需要这个(2026-09-04,公子问「Obsidian 作为可视化记忆是不是需要定期查重」):

  **需要的不是查重,是回收。** 查下来:
  - 重复不是"用久了自然会重",是写入端的 bug —— kanban_sync 在 HCC 拉取失败时
    把空索引当成"库里没有",全量重插;一小时一轮,库里攒出 1224 条重复
    (唯一任务只有 36 个),Obsidian 忠实导出成 758 个重复 md。
    洞已堵在 HanyanOS/memory/{hcc_client,kanban_sync}.py,存量已清。
    → 那不需要"定期查重",需要的是别再造。
  - 但**确有一个需要定期跑的缺口**:core/sync_engine.py 里**没有任何删除逻辑**,
    导出只增不删。记忆被 discard(做梦丢弃、降噪器丢弃、手工删)之后,
    对应的 md 永远留在 vault 里。实测 1252 个带 uuid 的文件里 **510 个是孤儿**。
    删除是持续发生的事件,所以这一条必须定期做。

**默认 dry-run。** --apply 才动;不硬删,移到 vault 内的 _trash/ 下,可回滚。
"""
import asyncio, re, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select

from core.config import core_settings
from gateway.core.database import async_session
from gateway.models import Memory

ID_RE = re.compile(r"^id:\s*([0-9a-f-]{36})", re.M)


async def main(apply: bool) -> None:
    root = Path(core_settings.qmd_dir).expanduser()
    trash = root / "_trash" / datetime.now().strftime("%Y%m%d-%H%M")
    files: dict[str, Path] = {}
    for p in root.rglob("*.md"):
        if "_trash" in p.parts:
            continue
        if m := ID_RE.search(p.read_text(encoding="utf-8", errors="ignore")):
            files[m.group(1)] = p

    async with async_session() as s:
        active = {str(x) for x in (await s.execute(
            select(Memory.id).where(Memory.status == "active"))).scalars()}

    orphans = {i: p for i, p in files.items() if i not in active}
    print(f"vault: {root}")
    print(f"  带 uuid 的 md {len(files)} · 对应记忆仍 active {len(files)-len(orphans)}")
    print(f"  **孤儿 {len(orphans)}**(记忆已 discarded 或不存在,文件还在)")
    for p in list(orphans.values())[:6]:
        print(f"    {p.relative_to(root)}")
    if not apply:
        print("\n(dry-run;加 --apply 才移走 —— 移到 _trash/ 不硬删)")
        return
    trash.mkdir(parents=True, exist_ok=True)
    for p in orphans.values():
        dest = trash / p.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
    print(f"\n已移走 {len(orphans)} 个 → {trash}")
    print("确认无误后可自行删除该目录;误伤了就整个搬回来。")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
