"""每周记忆审计:这周存了什么、降噪删了什么、dreaming 晋升了什么,输出一页 HTML。

动机(2026-09-16):库里 2.7 万条记忆、每晚自动软删除和自动晋升都在跑,但
没有任何一处能一眼看到"最近被删的都是些什么、有没有误删"。这个脚本就是
那只眼睛 —— 只读库,不改任何数据。

    uv run python scripts/memory_audit_report.py --days 7 --out ~/workspace/AICore/Reports/memory-audit.html
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
from datetime import datetime

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import core_settings  # noqa: E402

CSS = """
:root{--bg:#F5F6F3;--panel:#fff;--ink:#1B2220;--muted:#5E6A65;--rule:#DDE2DE;--accent:#0E6B60;--bad:#B0432F}
@media (prefers-color-scheme:dark){:root{--bg:#111513;--panel:#181D1B;--ink:#E4EBE8;--muted:#98A5A0;--rule:#2B3431;--accent:#52B9A9;--bad:#E27A64}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 "Noto Sans SC","PingFang SC",system-ui,sans-serif}
main{max-width:880px;margin:0 auto;padding:36px 20px 64px;display:flex;flex-direction:column;gap:28px}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:0 0 6px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:12px 14px}
.k{font-size:12px;color:var(--muted)}.v{font:600 22px/1.3 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:var(--panel)}
th,td{padding:8px 10px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12.5px}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.wrap{overflow-x:auto;border:1px solid var(--rule);border-radius:8px}
.snip{color:var(--muted);font-size:12.5px}
footer{color:var(--muted);font-size:12.5px;border-top:1px solid var(--rule);padding-top:12px}
"""


async def build(days: int) -> str:
    c = await asyncpg.connect(core_settings.database_url.replace("+asyncpg", ""))
    try:
        since = f"now() - interval '{days} days'"
        created = await c.fetch(f"select coalesce(type,'?') t, count(*) n from memories where created_at > {since} group by 1 order by 2 desc")
        discarded = await c.fetch(f"select coalesce(type,'?') t, count(*) n from memories where status='discarded' and updated_at > {since} group by 1 order by 2 desc")
        promoted = await c.fetch(f"""select left(replace(coalesce(summary,content),chr(10),' '),110) c, importance
            from memories where cast(tags as text) like '%promoted:deep:hcc%' and updated_at > {since}
            order by importance desc limit 12""")
        samples = await c.fetch(f"""select coalesce(type,'?') t, left(replace(content,chr(10),' '),140) c
            from memories where status='discarded' and updated_at > {since} order by random() limit 12""")
        totals = dict(await c.fetchrow("""select count(*) filter (where status='active') act,
            count(*) filter (where status='discarded') disc, count(*) total from memories"""))
        runs = await c.fetch(f"select phase, count(*) n from dream_runs where started_at > {since} group by 1")
    finally:
        await c.close()

    def rows(rs, cols):
        return "".join("<tr>" + "".join(f'<td class="{cl}">{html.escape(str(r[k]))}</td>' for k, cl in cols) + "</tr>" for r in rs)

    esc = html.escape
    return f"""<!doctype html><meta charset="utf-8"><title>记忆审计 · 最近 {days} 天</title>
<style>{CSS}</style><main>
<h1>记忆审计 · 最近 {days} 天</h1>
<div class="cards">
  <div class="card"><div class="k">活跃记忆</div><div class="v">{totals['act']}</div></div>
  <div class="card"><div class="k">已丢弃(可恢复)</div><div class="v">{totals['disc']}</div></div>
  <div class="card"><div class="k">本期新增</div><div class="v">{sum(r['n'] for r in created)}</div></div>
  <div class="card"><div class="k">本期被降噪丢弃</div><div class="v">{sum(r['n'] for r in discarded)}</div></div>
  <div class="card"><div class="k">做梦运行</div><div class="v">{sum(r['n'] for r in runs)}</div></div>
</div>
<section><h2>新增 / 丢弃(按类型)</h2><div class="wrap"><table>
<thead><tr><th>类型</th><th class="n">新增</th><th class="n">丢弃</th></tr></thead><tbody>
{"".join(f"<tr><td>{esc(t)}</td><td class='n'>{dict((r['t'], r['n']) for r in created).get(t,0)}</td><td class='n'>{dict((r['t'], r['n']) for r in discarded).get(t,0)}</td></tr>" for t in dict.fromkeys([r['t'] for r in created] + [r['t'] for r in discarded]))}
</tbody></table></div></section>
<section><h2>被丢弃的样本(抽查有没有误删)</h2><div class="wrap"><table>
<thead><tr><th>类型</th><th>内容开头</th></tr></thead><tbody>
{rows(samples, [('t',''),('c','snip')])}
</tbody></table></div></section>
<section><h2>本期被做梦晋升的记忆</h2><div class="wrap"><table>
<thead><tr><th>内容开头</th><th class="n">重要度</th></tr></thead><tbody>
{rows(promoted, [('c','snip'),('importance','n')])}
</tbody></table></div></section>
<footer>生成于 {datetime.now():%Y-%m-%d %H:%M} · 只读脚本,不改动任何记忆 · 恢复误删:
<code>update memories set status='active' where id='...'</code></footer>
</main>"""


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out", default=os.path.expanduser("~/workspace/AICore/Reports/memory-audit.html"))
    a = p.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w").write(asyncio.run(build(a.days)))
    print("写好:", a.out)
