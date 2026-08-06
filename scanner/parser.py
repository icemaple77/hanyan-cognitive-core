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

try:
    import pdf_inspector  # type: ignore
except ImportError:  # pragma: no cover - pdf_inspector is optional
    pdf_inspector = None  # type: ignore


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


# pdf_inspector classifies a PDF as one of these four types. Only
# "text_based" and "mixed" carry an extractable text layer that
# pdf_inspector.process_pdf() turns into markdown; "scanned" and
# "image_based" PDFs have no text layer (would need OCR) and produce no
# markdown, so they are not indexable as-is.
_PDF_INDEXABLE_TYPES = {"text_based", "mixed"}


class PdfSkipped(Exception):
    """Raised by :func:`parse_pdf_file` for a PDF with no extractable text.

    Callers (the indexer) should catch this and skip the file rather than
    treating it as an error — a scanned/image-based PDF is expected input,
    not a failure.
    """

    def __init__(self, pdf_type: str, page_count: int):
        self.pdf_type = pdf_type
        self.page_count = page_count
        super().__init__(f"no extractable text layer (pdf_type={pdf_type}, pages={page_count})")


def parse_pdf_file(path: Path) -> ParsedDocument:
    """Extract and parse the PDF file at *path* into a :class:`ParsedDocument`.

    Uses ``pdf_inspector`` (Rust-backed) to classify the PDF and convert its
    text layer to markdown. PDFs have no frontmatter, so ``metadata`` instead
    carries the ``pdf_type``/``page_count`` classification.

    Args:
        path: Filesystem path to a PDF file.

    Raises:
        RuntimeError: if the ``pdf_inspector`` package is not installed.
        PdfSkipped: if the PDF has no extractable text layer (scanned or
            image-based) — there is nothing to index.
    """
    if pdf_inspector is None:
        raise RuntimeError("pdf_inspector is not installed; run `pip install pdf-inspector`")

    result = pdf_inspector.process_pdf(str(path))
    if result.pdf_type not in _PDF_INDEXABLE_TYPES or not result.markdown:
        raise PdfSkipped(result.pdf_type, result.page_count)

    title = str(result.title or "").strip() or path.stem
    content = result.markdown.strip()

    return ParsedDocument(
        title=title,
        content=content,
        tags=[],
        metadata={"pdf_type": result.pdf_type, "page_count": result.page_count},
    )
