"""HCC v2 Knowledge Layer core modules.

This package contains the working-memory, event and knowledge-document
components introduced with the v2 evolution:

* :mod:`core.redis_manager` -- Redis-backed working memory manager.
* :mod:`core.event_bus`     -- Redis Pub/Sub event bus with typed events.
* :mod:`core.qmd_generator` -- PostgreSQL -> markdown knowledge generator.
"""

from core.config import CoreSettings, core_settings

__all__ = ["CoreSettings", "core_settings"]
