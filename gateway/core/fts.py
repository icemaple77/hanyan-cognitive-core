"""Tokenization helpers for PostgreSQL full-text search (BM25-style ranking).

Postgres' built-in text search parser splits CJK runs into one lexeme per
contiguous run of ideographs (no word segmentation), so ``to_tsvector`` on raw
Chinese content is nearly useless for ranking. We pre-segment with jieba
(search-mode, i.e. also emits sub-word n-grams for better recall) and feed the
*already space-separated* tokens into ``to_tsvector('simple', ...)``. The
``simple`` config then just lowercases and splits on whitespace/punctuation,
which is exactly what we want since jieba already did the real segmentation
work. English/ASCII tokens pass through jieba unchanged, so this is safe for
mixed zh/en content.

The same function must be used for both indexing (``search_text`` column) and
querying (the search term), otherwise tokens won't line up.
"""

from __future__ import annotations

import re

import jieba

__all__ = [
    "tokenize_for_fts",
    "build_search_text",
    "bm25_query_tokens",
    "BM25_MAX_QUERY_TOKENS",
    "BM25_MAX_OR_QUERY_TOKENS",
]

# BM25 query width cap (used by MemoryService/DocumentService keyword search).
# ``plainto_tsquery`` ANDs every token together, so a very long query (e.g. a
# whole chat message forwarded as the search text) builds a tsquery whose
# executor recursion exceeds Postgres' ``max_stack_depth`` and raises
# ``asyncpg.exceptions.StatementTooComplexError: stack depth limit exceeded`` —
# which failed /api/v1/context outright (observed 2026-09-12 with ~20k+ char
# queries). An AND over hundreds of tokens also matches nothing in practice, so
# capping is strictly better for recall too.
BM25_MAX_QUERY_TOKENS = 128

# Width cap for the OR fallback tier (see ``keyword_search_bm25``). The AND
# tier runs ``plainto_tsquery`` on up to BM25_MAX_QUERY_TOKENS tokens, i.e. a
# left-deep AND tree of depth ~128 — that is already proven to run (see above).
# The OR tier is built as a chain of ``tsquery || tsquery`` (one per token), so
# its depth equals the token count too; keeping it at half the AND cap bounds
# the executor recursion well under Postgres' ``max_stack_depth`` while still
# covering a full pasted-paragraph query. OR only ever runs when AND matched
# zero rows, so a smaller cap costs nothing in the common case.
BM25_MAX_OR_QUERY_TOKENS = 64

_HAS_WORDCHAR_RE = re.compile(r"\w", re.UNICODE)


def tokenize_for_fts(text: str) -> str:
    """Segment ``text`` into a space-joined token string for tsvector input."""
    if not text:
        return ""
    tokens = [t.strip() for t in jieba.cut_for_search(text)]
    tokens = [t for t in tokens if _HAS_WORDCHAR_RE.search(t)]
    return " ".join(tokens)


def bm25_query_tokens(query: str, max_tokens: int = BM25_MAX_QUERY_TOKENS) -> list[str]:
    """Tokenize a search query the same way ``search_text`` was built.

    Returns the capped token list (leading tokens win — they carry the actual
    search intent, trailing ones are usually pasted context/log). Shared by the
    AND and OR tiers of :meth:`MemoryService.keyword_search_bm25` so both sides
    of ``@@`` line up for mixed zh/en content.
    """
    if not query:
        return []
    tokens = tokenize_for_fts(query).split()
    return tokens[:max_tokens]


def build_search_text(content: str | None, summary: str | None, tags: list | None) -> str:
    """Build the tokenized blob stored in ``Memory.search_text``."""
    parts = [content or "", summary or ""]
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    raw = "\n".join(p for p in parts if p)
    return tokenize_for_fts(raw)
