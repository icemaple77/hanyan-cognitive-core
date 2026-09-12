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

__all__ = ["tokenize_for_fts", "build_search_text", "BM25_MAX_QUERY_TOKENS"]

# BM25 query width cap (used by MemoryService/DocumentService keyword search).
# ``plainto_tsquery`` ANDs every token together, so a very long query (e.g. a
# whole chat message forwarded as the search text) builds a tsquery whose
# executor recursion exceeds Postgres' ``max_stack_depth`` and raises
# ``asyncpg.exceptions.StatementTooComplexError: stack depth limit exceeded`` —
# which failed /api/v1/context outright (observed 2026-09-12 with ~20k+ char
# queries). An AND over hundreds of tokens also matches nothing in practice, so
# capping is strictly better for recall too.
BM25_MAX_QUERY_TOKENS = 128

_HAS_WORDCHAR_RE = re.compile(r"\w", re.UNICODE)


def tokenize_for_fts(text: str) -> str:
    """Segment ``text`` into a space-joined token string for tsvector input."""
    if not text:
        return ""
    tokens = [t.strip() for t in jieba.cut_for_search(text)]
    tokens = [t for t in tokens if _HAS_WORDCHAR_RE.search(t)]
    return " ".join(tokens)


def build_search_text(content: str | None, summary: str | None, tags: list | None) -> str:
    """Build the tokenized blob stored in ``Memory.search_text``."""
    parts = [content or "", summary or ""]
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    raw = "\n".join(p for p in parts if p)
    return tokenize_for_fts(raw)
