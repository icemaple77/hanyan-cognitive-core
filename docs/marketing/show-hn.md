# Show HN post

**Title (76 chars):**

```
Show HN: HCC – local-first, cross-agent memory layer (98% cache hit, $40/mo)
```

**Body:**

```
I kept re-solving the same problem every time I switched coding agents: Claude
Code forgets everything on a new session, my OpenClaw setup has its own memory
silo, and neither one shares context with the other. So I built HCC — a single
REST service that any agent can call over HTTP to get persistent memory,
instead of each agent bolting on its own incompatible store.

It's local-first by design: Postgres 17 + pgvector for storage, Ollama for
embeddings (qwen3-embedding, 1024-dim, decent multilingual/Chinese support),
zero required cloud dependency. Retrieval is BM25 (full-text + jieba) + vector
search fused with RRF, with an optional cross-encoder rerank pass.

Two things I haven't seen elsewhere: a nightly "dream" cycle (Light → REM →
Deep, three idempotent stages that cluster and de-duplicate the day's
memories into consolidated knowledge, with a narrated dream journal written to
Obsidian), and a 6-dimensional emotion state machine that persists across
sessions and biases retrieval toward mood-consistent memories.

Real numbers from my own 30-day production usage: 98% prompt cache hit rate,
¥285.67 total (~$40, ~$1.3/day), because memory context gets assembled into a
stable, reusable structure instead of getting rebuilt (and rebilled) every
turn.

MIT licensed. Ships an OpenClaw plugin and a Claude Code MCP server; anything
that speaks HTTP can use it. One-line install script included.

https://github.com/icemaple77/hanyan-cognitive-core
```
