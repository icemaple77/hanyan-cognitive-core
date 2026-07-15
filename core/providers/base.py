"""Provider SDK -- the abstract contract every HCC v2.1 data provider implements.

A *Provider* is a thin, uniform async facade over a concrete storage/knowledge
backend (PostgreSQL memories, QMD markdown files, future vector stores, ...).
Higher layers (managers, context builder, gateway routes) only ever talk to the
:class:`Provider` interface and the plain dataclasses declared here, which keeps
the rest of the system decoupled from any particular backend.

All request/response payloads are ordinary :mod:`dataclasses` so they are cheap
to construct, trivially serialisable, and free of any backend imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SearchQuery",
    "SearchResult",
    "StoreData",
    "StoreResult",
    "UpdateData",
    "HealthStatus",
    "ProviderMetadata",
    "Provider",
]


# ---------------------------------------------------------------------------
# Request / response dataclasses
# ---------------------------------------------------------------------------
@dataclass
class SearchQuery:
    """A normalised search request passed to :meth:`Provider.search`.

    Attributes
    ----------
    query:
        Free-text keyword/phrase to match. Empty string means "no keyword
        filter" (list-style browsing).
    user_id:
        Optional owner filter. ``None`` searches across all users.
    type:
        Optional category/type filter (e.g. ``"person"``, ``"project"``).
    limit:
        Maximum number of items to return.
    offset:
        Number of leading items to skip (pagination).
    """

    query: str = ""
    user_id: str | None = None
    type: str | None = None
    limit: int = 20
    offset: int = 0


@dataclass
class SearchResult:
    """The result of a :meth:`Provider.search` call.

    Attributes
    ----------
    items:
        JSON-serialisable dicts, each describing one matched record.
    total:
        Total number of matches (ignoring ``limit``/``offset``) when known,
        otherwise the length of ``items``.
    provider:
        Name of the provider that produced the result.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    provider: str = ""


@dataclass
class StoreData:
    """Payload for creating a new record via :meth:`Provider.store`.

    Mirrors the durable-memory shape used throughout HCC so it maps cleanly
    onto both the PostgreSQL ``Memory`` model and QMD markdown documents.
    """

    content: str
    user_id: str
    type: str = "general"
    summary: str = ""
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    source: str = "api"


@dataclass
class StoreResult:
    """The result of a :meth:`Provider.store` (or update) call.

    Attributes
    ----------
    id:
        Identifier of the stored/updated record (UUID, file path, ...).
    success:
        ``True`` when the operation persisted successfully.
    provider:
        Name of the provider that handled the write.
    """

    id: str
    success: bool = True
    provider: str = ""


@dataclass
class UpdateData:
    """Partial-update payload for :meth:`Provider.update`.

    Every field except :attr:`id` is optional; ``None`` means "leave
    unchanged". This lets callers patch a single attribute without having to
    read-modify-write the whole record.
    """

    id: str
    content: str | None = None
    summary: str | None = None
    importance: float | None = None
    tags: list[str] | None = None
    status: str | None = None


@dataclass
class HealthStatus:
    """Backend liveness/health snapshot returned by :meth:`Provider.health`."""

    healthy: bool
    version: str = ""
    provider_name: str = ""
    latency_ms: float = 0.0


@dataclass
class ProviderMetadata:
    """Static description of a provider returned by :meth:`Provider.metadata`.

    Attributes
    ----------
    name:
        Stable provider identifier (e.g. ``"memory"``, ``"knowledge_qmd"``).
    version:
        Provider implementation/schema version string.
    capabilities:
        List of capability tags advertised by the provider, e.g.
        ``["search", "store", "update", "delete"]``.
    config:
        Non-secret, JSON-serialisable configuration snapshot.
    """

    name: str
    version: str = "0.1.0"
    capabilities: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------
class Provider(ABC):
    """Abstract async interface implemented by every HCC data provider.

    Concrete providers wrap a specific backend and translate between that
    backend's native API and the neutral dataclasses declared in this module.
    Implementations must be safe to call concurrently and should never leak
    backend-specific exceptions past their public methods without wrapping or
    documenting them.
    """

    #: Human/stable provider name; subclasses should override.
    name: str = "provider"

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResult:
        """Return records matching ``query`` (see :class:`SearchResult`)."""

    @abstractmethod
    async def store(self, data: StoreData) -> StoreResult:
        """Persist a new record described by ``data``."""

    @abstractmethod
    async def update(self, data: UpdateData) -> StoreResult:
        """Apply a partial update to an existing record."""

    @abstractmethod
    async def delete(self, id: str) -> bool:
        """Delete (or soft-delete) the record identified by ``id``.

        Returns ``True`` when a record was affected, ``False`` otherwise.
        """

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Probe the underlying backend and report its health."""

    @abstractmethod
    async def metadata(self) -> ProviderMetadata:
        """Return static metadata describing this provider."""
