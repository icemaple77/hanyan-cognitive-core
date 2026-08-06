# Awesome-list submissions

## 1. punkpeye/awesome-mcp-servers — submitted ✅

The canonical MCP servers list (~92k stars), "Knowledge & Memory" category. HCC ships a stdio
MCP server (`mcp/server.py`), so this is a direct fit. Their `CONTRIBUTING.md` explicitly
fast-tracks automated/agent PRs (tag the title with `🤖🤖🤖`), so this was submitted directly.

- Fork: https://github.com/icemaple77/awesome-mcp-servers (branch `add-hanyan-cognitive-core`)
- PR: https://github.com/punkpeye/awesome-mcp-servers/pull/11615
- Entry added (end of the Knowledge & Memory section, before "Legal"):

  ```md
  - [icemaple77/hanyan-cognitive-core](https://github.com/icemaple77/hanyan-cognitive-core) 🐍 🏠 - Local-first, cross-agent memory layer (Hermes/OpenClaw/Claude Code) behind one REST API. Hybrid BM25+pgvector+RRF retrieval with optional Qwen3 rerank, a three-stage "dream" cycle that clusters and de-dupes daily memories into consolidated knowledge with a narrative journal written to Obsidian, and a persistent 6-dimension emotion state machine that biases retrieval by mood. Postgres 17 + pgvector + Ollama embeddings, zero required cloud dependency. `./install.sh`
  ```

No further action needed unless a maintainer requests changes on the PR.

---

## 2. awesome-selfhosted — researched, NOT submitted (ineligible until ~2026-12-05)

`awesome-selfhosted-data` (the data source for awesome-selfhosted.net and the README-based
list) requires, per their addition template
(`.github/ISSUE_TEMPLATE/addition.md`):

> Any software project you are adding was first released more than 4 months ago.

The public GitHub repo (`icemaple77/hanyan-cognitive-core`) was created **2026-08-05** — one day
before this was researched. It doesn't meet the 4-month bar (would clear it around
**2026-12-05**). Submitting now would likely get closed on that criterion alone, so I did not
open a PR. Content is ready below for whenever you want to submit it yourself (or ping me after
December and I'll do it then).

**How to submit when eligible** (no fork needed — GitHub's web UI can create the file + PR
directly):
1. Go to https://github.com/awesome-selfhosted/awesome-selfhosted-data/new/master/software
2. File name: `hanyan-cognitive-core.yml`
3. Paste the YAML below, adjust anything that's changed by then
4. Commit as a new branch + PR (GitHub does this for you in the web editor)

```yaml
name: "Hanyan Cognitive Core (HCC)"
website_url: "https://github.com/icemaple77/hanyan-cognitive-core"
source_code_url: "https://github.com/icemaple77/hanyan-cognitive-core"
description: "Local-first, cross-agent memory layer with hybrid retrieval, dream-style memory consolidation, and a persistent emotion state machine."
licenses:
  - MIT
platforms:
  - Python
  - Docker
tags:
  - Generative AI (GenAI)
  - Knowledge management tools
depends_3rdparty: false
```

(`depends_3rdparty: false` because the core memory/retrieval path runs entirely on local
Postgres+pgvector+Ollama; only worth flipping to `true` if the README ends up recommending a
cloud LLM by default.)

---

## Other lists considered and passed on

- **e2b-dev/awesome-ai-agents** — scoped to autonomous agent frameworks/products, not memory
  infrastructure; HCC isn't a fit for the category structure.
- **steven2358/awesome-generative-ai** — no dedicated memory/agent-tooling category; closest fit
  (`Developer tools` under `Coding`) is a stretch.
- **tfatykhov/awesome-agent-memory** — memory-for-agents themed, but it's a papers/research
  reading list (arXiv links only), not a projects list; wrong content type for a project
  submission.
- **dhamaniasad/awesome-postgres** — lists Postgres tools/extensions/clients, not applications
  built on top of Postgres; HCC would be out of scope.
