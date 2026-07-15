"""Markdown parsing utilities for the HCC file scanner.

The parser turns a raw markdown document into a :class:`ParsedDocument` that
captures everything the absorber needs: a title, the body content, a set of
tags (derived from headings and frontmatter) and any frontmatter metadata.

Frontmatter is an optional block delimited by ``---`` lines at the very top of
the file, containing YAML key/value pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml is optional
    yaml = None  # type: ignore


# Matches a leading frontmatter block: "---\n...\n---\n".
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*\r?\n?",
    re.DOTALL,
)
# Matches ATX headings, e.g. "# Title" or "### Sub section".
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)


@dataclass
class ParsedDocument:
    """Structured representation of a parsed markdown file.

    Attributes:
        title: Best-effort document title (frontmatter ``title`` > first H1 >
            filename).
        content: The markdown body with frontmatter stripped.
        tags: Deduplicated list of tags gathered from frontmatter and headings.
        metadata: Raw frontmatter mapping (empty when absent).
    """

    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split *text* into (frontmatter mapping, remaining body).

    Returns an empty mapping and the original text when no frontmatter block is
    present or when YAML parsing fails.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    body = text[match.end():]
    raw = match.group("body")

    metadata: dict[str, Any] = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                metadata = loaded
        except yaml.YAMLError:
            metadata = {}
    else:  # pragma: no cover - fallback parser when PyYAML is unavailable
        metadata = _parse_simple_yaml(raw)

    return metadata, body


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    """Very small ``key: value`` fallback used when PyYAML is not installed."""
    result: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip().strip("'\"")
    return result


def _extract_headings(body: str) -> list[str]:
    """Return the text of every markdown heading in *body*, in document order."""
    return [m.group(2).strip() for m in _HEADING_RE.finditer(body) if m.group(2).strip()]


def _normalize_tags(values: Any) -> list[str]:
    """Coerce a frontmatter tags value into a clean list of strings."""
    if values is None:
        return []
    if isinstance(values, str):
        parts = re.split(r"[,\s]+", values.strip())
        return [p for p in parts if p]
    if isinstance(values, (list, tuple, set)):
        return [str(v).strip() for v in values if str(v).strip()]
    return [str(values).strip()]


def parse_markdown(text: str, *, fallback_title: str = "untitled") -> ParsedDocument:
    """Parse a markdown *text* into a :class:`ParsedDocument`.

    Args:
        text: Raw markdown file contents.
        fallback_title: Title to use when neither frontmatter nor an H1 heading
            provides one (typically the file stem).

    Returns:
        A :class:`ParsedDocument`. Parsing never raises for malformed markdown;
        the worst case is a document with just its raw content.
    """
    metadata, body = _parse_frontmatter(text)
    headings = _extract_headings(body)

    # Title precedence: frontmatter title > first heading > fallback.
    title = str(metadata.get("title") or "").strip()
    if not title:
        title = headings[0] if headings else fallback_title

    # Tags: frontmatter "tags" merged with every heading text.
    tags = _normalize_tags(metadata.get("tags"))
    tags.extend(headings)

    seen: set[str] = set()
    unique_tags: list[str] = []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            unique_tags.append(tag)

    return ParsedDocument(
        title=title,
        content=body.strip(),
        tags=unique_tags,
        metadata=metadata,
    )


def parse_file(path: Path) -> ParsedDocument:
    """Read and parse the markdown file at *path*.

    Args:
        path: Filesystem path to a markdown file.

    Returns:
        The parsed document, using the file stem as the fallback title.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_markdown(text, fallback_title=path.stem)
