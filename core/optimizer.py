"""Workspace Optimizer — manages Agent workspace lifecycle."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BOOTSTRAP_FILES = {"AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md", "CLAUDE.md",
                   "IDENTITY.md", "HEARTBEAT.md", "ARCHITECTURE.md", "CROSS_AGENT_COMMS.md",
                   "README.md", "INVENTORY.md", "OPENCLAW_SKILLS.md"}

ABSORBABLE_PATTERNS = [
    r"DREAMS?\.md",
    r"dream/.*\.md",
    r"memory/.*\.md",
    r"memory/.*",
    r"memory$",
    r"notes?/.*\.md",
    r"snapshots?/.*\.md",
]


class WorkspaceOptimizer:
    def __init__(self, workspace_dir: str | Path | None = None):
        self._workspace_dir = Path(workspace_dir) if workspace_dir else None

    @property
    def workspace(self) -> Path | None:
        return self._workspace_dir

    @workspace.setter
    def workspace(self, path: str | Path) -> None:
        self._workspace_dir = Path(path)

    def scan_for_absorbable(self) -> list[Path]:
        if not self._workspace_dir or not self._workspace_dir.exists():
            return []
        absorbable: list[Path] = []
        for f in self._workspace_dir.rglob("*"):
            if not f.is_file() or f.suffix != ".md":
                continue
            rel = f.relative_to(self._workspace_dir)
            rel_str = rel.as_posix()
            if rel.name in BOOTSTRAP_FILES:
                continue
            for pattern in ABSORBABLE_PATTERNS:
                if re.match(pattern, rel_str):
                    absorbable.append(f)
                    break
        return sorted(absorbable)

    def scan_all_files(self) -> dict[str, list[Path]]:
        if not self._workspace_dir or not self._workspace_dir.exists():
            return {"bootstrap": [], "absorbable": [], "other": []}
        result = {"bootstrap": [], "absorbable": [], "other": []}
        s = set(self.scan_for_absorbable())
        for f in self._workspace_dir.rglob("*.md"):
            if f.name in BOOTSTRAP_FILES:
                result["bootstrap"].append(f)
            elif f in s:
                result["absorbable"].append(f)
            else:
                result["other"].append(f)
        return result

    def delete_absorbed_files(self, dry_run: bool = False) -> list[Path]:
        to_delete = self.scan_for_absorbable()
        if dry_run:
            return to_delete
        deleted = []
        for f in to_delete:
            try:
                f.unlink()
                deleted.append(f)
            except OSError as e:
                logger.warning("optimizer: failed to delete %s: %s", f, e)
        return deleted

    def _fetch_hcc_data(self, api_base: str, agent_id: str = "main") -> dict[str, Any]:
        """Pull live data from HCC for bootstrap file generation."""
        import httpx
        result = {"user_profile": "", "recent": "", "emotion": "", "personality": ""}

        try:
            r = httpx.post(f"{api_base}/api/v1/memory/search", json={
                "type": "user_profile",
                "shared": True, "limit": 5
            }, timeout=5)
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    if item.get("type") == "user_profile":
                        result["user_profile"] = item.get("content", "")[:2000]
                        break

            r = httpx.get(f"{api_base}/api/v1/memory/recent?limit=5", timeout=5)
            if r.status_code == 200:
                items = r.json().get("items", [])
                summaries = [i.get("summary", "")[:100] for i in items if i.get("summary")]
                if summaries:
                    result["recent"] = "\n".join(f"- {s}" for s in summaries[:5])

            r = httpx.get(f"{api_base}/api/v1/emotion/state", timeout=5)
            if r.status_code == 200:
                d = r.json()
                state = d.get("state", {})
                if state:
                    result["emotion"] = ", ".join(f"{k}={v:.2f}" for k, v in state.items())

            r = httpx.get(f"{api_base}/api/v1/personality/summary", timeout=5)
            if r.status_code == 200:
                d = r.json()
                traits = d.get("top_traits", [])
                if traits:
                    result["personality"] = ", ".join(traits)
        except Exception as e:
            logger.warning("optimizer: failed to fetch HCC data: %s", e)

        return result

    def generate_bootstrap(self, memory_count: int = 0,
                           preferences: list[str] | None = None,
                           traits: list[str] | None = None,
                           hcc_api_base: str = "") -> dict[str, str]:
        """Generate bootstrap files. Queries HCC live data when api_base is set."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        prefs = ", ".join(preferences[:5]) if preferences else "none tracked"
        trait_str = ", ".join(traits[:5]) if traits else "learning..."

        hcc = self._fetch_hcc_data(hcc_api_base) if hcc_api_base else {}

        user_section = ""
        if hcc.get("user_profile"):
            user_section = f"\n## 👤 User Profile\n\n{hcc['user_profile']}\n"

        recent_section = ""
        if hcc.get("recent"):
            recent_section = f"\n## 📋 Recent Context\n\n{hcc['recent']}\n"

        emotion_section = ""
        if hcc.get("emotion"):
            emotion_section = f"\n**Emotion:** {hcc['emotion']}\n"

        personality_section = ""
        if hcc.get("personality"):
            personality_section = f"\n**Personality:** {hcc['personality']}\n"

        hcc_api_docs = f"""
## 🧠 HCC API

Long-term memory is managed by **Hanyan Cognitive Core (HCC)** at `{hcc_api_base}`.

### Read
```bash
curl -s -X POST {hcc_api_base}/api/v1/context \\
  -H "Content-Type: application/json" \\
  -d '{{"query":"<topic>","user_id":"michael","agent_id":"main","include_emotion":true}}'
```

### Write
```bash
curl -s -X POST {hcc_api_base}/api/v1/memory/store \\
  -H "Content-Type: application/json" \\
  -d '{{"content":"<fact>","summary":"<summary>","agent_id":"main","shared":true}}'
```
""" if hcc_api_base else ""

        return {
            "MEMORY.md": f"""# Memory Management

{user_section}{recent_section}
## Policy

- **HCC is the single source of truth** for all long-term memory.
- Do NOT duplicate long-term memories in this file.
- This file is regenerated by HCC after each session.
{hcc_api_docs}
## Current state

*Last optimized: {now}*
*Memories in HCC: {memory_count}*
*Active preferences: {prefs}*
{emotion_section}
---

*This file is auto-generated by HCC.*
""",
            "SOUL.md": f"""# Identity & Personality

{personality_section}
Your identity and personality are maintained by **Hanyan Cognitive Core (HCC)**.

## Guidelines

- Personality is NOT stored in this file.
- HCC tracks preferences, emotional baselines, and trait evolution.
- Query HCC: `curl -s {hcc_api_base}/api/v1/personality/summary`

## Current state

*Last synced: {now}*
*Active traits: {trait_str}*

---

*This file is auto-generated by HCC.*
""",
            "USER.md": f"""# User Profile

{hcc.get('user_profile', '')[:500] or 'User information is maintained by HCC.'}

## Policy

- Do NOT store user profile data in this file.
- HCC tracks user preferences, relationships, and history.
- Query HCC: `curl -s {hcc_api_base}/api/v1/context -d '{{"query":"user profile","user_id":"michael"}}'`

---

*This file is auto-generated by HCC.*
""",
        }

    def write_bootstrap(self, contents: dict[str, str]) -> list[Path]:
        if not self._workspace_dir:
            return []
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name, content in contents.items():
            path = self._workspace_dir / name
            path.write_text(content, encoding="utf-8")
            written.append(path)
        return written


_optimizer: WorkspaceOptimizer | None = None


def get_optimizer() -> WorkspaceOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = WorkspaceOptimizer()
    return _optimizer
