# r/selfhosted post

**Title:**

```
Self-hosted memory layer for AI agents — Postgres + pgvector + Ollama, zero cloud dependency, $0 API cost for retrieval
```

**Body:**

```
Been running a fully self-hosted "memory brain" for my AI agents (Claude
Code, a custom OpenClaw setup) for a few months and wanted to share it since
it's the kind of thing this sub tends to appreciate: no managed vector DB, no
embedding API, no SaaS memory product — just a Postgres instance and a REST
API on my own box.

**Stack:**
- PostgreSQL 17 + pgvector for storage and vector search
- Ollama running local embedding + a small model for background noise
  filtering (auto-reviews low-confidence memories and soft-deletes junk
  without ever hard-deleting anything)
- Redis optional — if you don't run it, the event bus just degrades to an
  in-process broadcaster, no feature loss for a single-node setup
- Everything behind one FastAPI gateway, `docker-compose.yml` included (api +
  mcp + db + redis), or run it bare-metal with a venv

**Why self-host this instead of using a hosted memory product:** the whole
point was to stop leaking every memory/conversation snippet to a third-party
API just to embed it. With local embeddings the entire retrieval path never
leaves my machine — the only external call is the actual chat completion,
and that's my choice which provider to use (or none, if you point it at a
local LLM too). My real production billing over 30 days: ~$40 total, almost
entirely from the completion calls, $0 from retrieval/embedding since that's
all local.

A couple of things beyond plain storage that might interest self-hosters
specifically:
- **Bidirectional Obsidian sync** — memories and a nightly auto-generated
  "dream journal" get written out as Markdown into your own vault, so your
  data isn't trapped behind an API even if you stop running the service
  tomorrow
- **Read-only vault API** with strict path containment (rejects `..`,
  symlink escapes, absolute paths) if you want to expose a browsing endpoint
  without handing over filesystem access
- One-line installer (`./install.sh`) that detects Docker and uses it for
  Postgres/pgvector if available, otherwise falls back to native
  Homebrew/apt install — there's also a `--no-docker` fully bare-metal path
  if you'd rather not containerize anything

MIT licensed. Repo: https://github.com/icemaple77/hanyan-cognitive-core
```
