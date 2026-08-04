#!/usr/bin/env python3
"""CLI wrapper for :mod:`core.agent_export` — run from cron or by hand.

    cd /home/user/workspace/projects/HCC
    set -a; source .env; set +a
    .venv/bin/python scripts/export_agent_memories.py

Equivalent to ``python -m core.agent_export``; this thin wrapper exists so
the export lives in ``scripts/`` alongside ``backup_hcc.sh`` /
``index_documents.py`` for anyone scanning that directory for schedulable
jobs, rather than only being discoverable via ``python -m``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent_export import main  # noqa: E402

if __name__ == "__main__":
    main()
