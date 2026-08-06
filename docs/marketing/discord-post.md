# Discord 推广文案（Hermes #plugins-skills-and-skins）

## 标题
**[Plugin] hermes-hcc-memory — Local-first unified memory layer for Hermes**

## 正文

Hi everyone! I've been running Hermes with a custom memory provider for a while and finally packaged it up for the community: **hermes-hcc-memory**.

**What it is**: A memory plugin that backs Hermes with **HCC (Hanyan Cognitive Core)** — a fully local, cross-agent memory layer. Instead of a fixed-size MEMORY.md, your agent gets:

- 🧠 **Hybrid semantic retrieval** — BM25 + jieba + pgvector + RRF + Qwen3 rerank, with local embedding (qwen3-embedding, no API cost)
- 🔄 **Cross-agent shared memory** — the same memory pool serves Hermes, OpenClaw, and Claude Code (with per-agent isolation)
- ✨ **Dreaming system** — three-phase (Light/REM/Deep) memory consolidation with narrative dream diaries
- 💖 **Emotion system** — 6-dimension emotion state machine that warms into every session
- 💰 **Cost-efficient** — 98% input cache-hit rate, local embedding = $0 API cost
- 🔌 **One-line install**:
  ```
  hermes plugins install icemaple77/hermes-hcc-memory --enable
  hermes config set memory.provider hcc
  ```

The core is MIT-licensed and fully self-hosted (PostgreSQL + Ollama, no cloud dependency). Main repo: https://github.com/icemaple77/hanyan-cognitive-core

Happy to answer questions — especially about the dreaming/emotion design or the OpenClaw/Hermes dual integration!

---

## 备选短版（Discord 字数限制友好）

**[Plugin] hermes-hcc-memory — local-first cross-agent memory layer for Hermes 🧠**
Fully local memory backend (PG + Ollama, zero API cost): hybrid semantic retrieval (BM25+pgvector+RRF+Qwen3 rerank), 98% cache-hit, dreaming + emotion systems, shared memory across Hermes/OpenClaw/Claude Code.
`hermes plugins install icemaple77/hermes-hcc-memory --enable && hermes config set memory.provider hcc`
MIT. Repo: github.com/icemaple77/hanyan-cognitive-core — feedback welcome!
