"""Absorb parsed markdown documents into the HCC Memory API.

The :class:`Absorber` maps a :class:`~scanner.parser.ParsedDocument` onto the
Memory API's ``MemoryCreate`` payload and POSTs it to ``/memory/store``. It is
an async context manager so the underlying :class:`httpx.AsyncClient` is reused
across many files and cleanly closed afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from scanner.config import ScannerSettings
from scanner.parser import ParsedDocument


@dataclass
class AbsorbResult:
    """Outcome of attempting to absorb a single document.

    Attributes:
        ok: Whether the document was stored (or would be stored in dry-run).
        memory_id: The id returned by the API, when available.
        error: Human-readable error message when ``ok`` is ``False``.
        dry_run: Whether this result came from a dry-run (nothing was stored).
    """

    ok: bool
    memory_id: str | None = None
    error: str | None = None
    dry_run: bool = False


class Absorber:
    """Send parsed markdown documents to the HCC Memory API over HTTP."""

    def __init__(
        self,
        settings: ScannerSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the absorber.

        Args:
            settings: Scanner settings providing the API URL, timeout and the
                ``user_id``/``source`` labels applied to stored memories.
            client: Optional pre-built async HTTP client (useful for testing).
                When omitted, one is created lazily on first use.
        """
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "Absorber":
        """Enter the async context, creating an HTTP client if needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.request_timeout)
            self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the async context, closing the client if we created it."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance owns it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def build_payload(
        self, doc: ParsedDocument, extra_tags: list[str] | None = None
    ) -> dict[str, Any]:
        """Build the ``MemoryCreate`` request body for *doc*.

        Args:
            doc: The parsed markdown document.
            extra_tags: Additional tags to merge in (e.g. a relative path tag).

        Returns:
            A JSON-serialisable dict matching the Memory API schema.
        """
        tags = list(doc.tags)
        if extra_tags:
            tags.extend(t for t in extra_tags if t)

        # Stored content keeps the title as an H1 so it round-trips nicely.
        content = doc.content
        if doc.title and not content.lstrip().startswith("#"):
            content = f"# {doc.title}\n\n{content}"

        importance = 0.5
        raw_importance = doc.metadata.get("importance")
        if isinstance(raw_importance, (int, float)):
            importance = max(0.0, min(1.0, float(raw_importance)))

        return {
            "user_id": self._settings.user_id,
            "type": str(doc.metadata.get("type", "note")),
            "content": content,
            "summary": doc.title,
            "importance": importance,
            "tags": tags,
            "source": self._settings.source,
        }

    async def absorb(
        self, doc: ParsedDocument, extra_tags: list[str] | None = None
    ) -> AbsorbResult:
        """Store a single parsed document via the Memory API.

        Network and HTTP errors are caught and returned as a failed
        :class:`AbsorbResult` rather than raised, so a single bad file never
        aborts an entire scan.

        Args:
            doc: The parsed markdown document to store.
            extra_tags: Optional extra tags to attach.

        Returns:
            An :class:`AbsorbResult` describing success or failure.
        """
        payload = self.build_payload(doc, extra_tags=extra_tags)

        if self._settings.dry_run:
            return AbsorbResult(ok=True, dry_run=True)

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.request_timeout)
            self._owns_client = True

        try:
            response = await self._client.post(
                self._settings.store_url, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200]
            return AbsorbResult(
                ok=False,
                error=f"HTTP {exc.response.status_code}: {body}",
            )
        except httpx.HTTPError as exc:
            return AbsorbResult(ok=False, error=f"request failed: {exc}")

        memory_id: str | None = None
        try:
            data = response.json()
            if isinstance(data, dict):
                memory_id = data.get("id")
        except ValueError:
            pass  # Non-JSON success body; still counts as stored.

        return AbsorbResult(ok=True, memory_id=memory_id)
