# r/LocalLLaMA post

**Title:**

```
Built a local-first hybrid retrieval memory layer for agents (BM25 + pgvector + RRF + local Qwen3 embeddings/rerank)
```

**Body:**

```
Sharing a project I've been running in production for a few months: HCC
(Hanyan Cognitive Core), a standalone memory service for LLM agents, built
around a fully local retrieval stack.

**Retrieval pipeline:**
- BM25 via Postgres full-text search + jieba tokenization (matters if you're
  doing anything in Chinese — standard tsvector tokenizers butcher it)
- Vector search via pgvector, embeddings from local Ollama
  (`qwen3-embedding:0.6b`, 1024-dim — noticeably better than
  `nomic-embed-text` on Chinese semantic similarity in my testing)
- Both fused with Reciprocal Rank Fusion (RRF), so you get keyword precision
  and semantic recall without picking one
- Optional Qwen3 cross-encoder reranking pass on top, gated behind a flag
  since it's the most expensive step and most queries don't need it
- Fallback: `HCC_EMBEDDING_PROVIDER=hash` gives you a deterministic
  zero-dependency embedding if you want to stand the whole thing up before
  wiring in a real model

**Why local-first mattered to me:** every embedding call in a naive
RAG/memory setup is a per-token API bill and a round-trip. Running embeddings
through Ollama means retrieval has zero marginal cost and zero external
dependency — the only cloud cost I have is the actual chat completion, which
I can point at whatever model I want (currently DeepSeek). My real 30-day
bill: 98% prompt cache hit rate, ~$40 total, because assembled memory context
is structured to be cache-stable across turns instead of rebuilt each time.

On top of retrieval it also does something I haven't seen in other memory
projects: a nightly three-stage "dream" consolidation (cluster → dedup →
distill, all idempotent) that turns scattered daily memories into durable
knowledge, plus a persistent 6-dim emotion state that weights retrieval by
mood consistency. Both entirely optional if you just want the retrieval
layer.

MIT licensed, Postgres 17 + pgvector + optional Redis/Ollama, one-line
install script (`./install.sh`, has a `--no-docker` pure-local path). Works
as a Claude Code MCP server or plain REST for anything else.

https://github.com/icemaple77/hanyan-cognitive-core

Curious what others are using for hybrid retrieval fusion locally — RRF has
been solid for me but I haven't rigorously benchmarked it against
learned-fusion approaches.
```
