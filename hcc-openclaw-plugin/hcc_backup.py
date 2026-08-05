#!/usr/bin/env python3
"""
HCC 数据每日备份 — 从 macmini 拉取 HCC 记忆数据备份到本机
- 通过 REST API 分页拉取全部记忆（recent 端点）
- 存为 JSONL 快照到本机 archive/hcc-backups/
- 保留最近 14 天
"""
import json
import os
import sys
import glob
import datetime
import urllib.request

HCC_BASE = os.environ.get("HCC_BASE_URL", "http://localhost:8000")
BACKUP_DIR = os.path.expanduser("~/.openclaw/workspace/archive/hcc-backups")
RETAIN_DAYS = 14

def hcc_get(path):
    req = urllib.request.Request(f"{HCC_BASE}/api/v1{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    out_path = os.path.join(BACKUP_DIR, f"hcc-memory-{date_str}.jsonl")

    # 分页拉取全部记忆（recent 端点，每页 200）
    all_items = []
    offset = 0
    page_size = 200
    while True:
        data = hcc_get(f"/memory/recent?limit={page_size}&offset={offset}")
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        total = data.get("total", len(all_items))
        offset += page_size
        if offset >= total or len(items) < page_size:
            break
        if len(all_items) > 20000:  # 安全上限
            print("⚠️ 超过 2 万条，停止拉取（安全上限）")
            break

    with open(out_path, "w", encoding="utf-8") as f:
        for item in all_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    size_mb = os.path.getsize(out_path) / 1048576
    print(f"✅ 备份完成: {len(all_items)} 条记忆 → {out_path} ({size_mb:.2f} MB)")

    # 清理过期备份
    cutoff = datetime.date.today() - datetime.timedelta(days=RETAIN_DAYS)
    removed = 0
    for f in glob.glob(os.path.join(BACKUP_DIR, "hcc-memory-*.jsonl")):
        try:
            fdate = datetime.date.fromisoformat(os.path.basename(f).replace("hcc-memory-", "").replace(".jsonl", ""))
            if fdate < cutoff:
                os.remove(f)
                removed += 1
        except Exception:
            continue
    if removed:
        print(f"🧹 清理 {removed} 个过期备份（保留 {RETAIN_DAYS} 天）")

    return 0

if __name__ == "__main__":
    sys.exit(main())
