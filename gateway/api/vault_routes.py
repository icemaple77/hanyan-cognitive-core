"""Read-only Obsidian vault browse API — GET /vault/list + GET /vault/read.

Lets an agent browse/read the exported knowledge base (QMD ``Knowledge/``,
per-agent ``agents/<agent_id>/``, the ``Dreams/`` diary, or any other file
under the vault) "per user instruction" without shelling out to the
filesystem directly. Every path is resolved and checked against
``HCC_VAULT_ROOT`` before touching disk — ``..`` segments, symlinks that
escape the root, and absolute paths are all rejected the same way (resolve,
then require the result to be ``HCC_VAULT_ROOT`` or a descendant of it).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from core.config import core_settings

router = APIRouter()

_MAX_READ_BYTES = 2 * 1024 * 1024  # 2MB — this is a note-reading API, not a file server


def _vault_root() -> Path:
    return Path(core_settings.vault_root).expanduser().resolve()


def _resolve_safe(rel_path: str) -> Path:
    """Resolve ``rel_path`` against the vault root, rejecting any escape."""
    root = _vault_root()
    # A leading "/" would make Path(root, rel) ignore root entirely — strip it
    # so "/etc/passwd" is treated as a relative (and thus confined) path too.
    cleaned = (rel_path or "").lstrip("/")
    candidate = (root / cleaned).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=400, detail="path escapes vault root")
    return candidate


@router.get("/vault/list", summary="List a vault directory (default: vault root)")
async def vault_list(path: str = Query(default="", description="Path relative to HCC_VAULT_ROOT")) -> dict:
    target = _resolve_safe(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory (use /vault/read)")

    root = _vault_root()
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        stat = child.stat()
        entries.append({
            "name": child.name,
            "path": str(child.relative_to(root)),
            "is_dir": child.is_dir(),
            "size": stat.st_size if child.is_file() else None,
            "mtime": stat.st_mtime,
        })
    return {"path": str(target.relative_to(root)) if target != root else "", "entries": entries}


@router.get("/vault/read", summary="Read a vault file's content")
async def vault_read(path: str = Query(..., description="Path relative to HCC_VAULT_ROOT")) -> dict:
    target = _resolve_safe(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="path is not a file (use /vault/list)")

    size = target.stat().st_size
    if size > _MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail=f"file too large ({size} bytes > {_MAX_READ_BYTES})")

    root = _vault_root()
    content = target.read_bytes().decode("utf-8", errors="replace")
    return {
        "path": str(target.relative_to(root)),
        "size": size,
        "content": content,
    }
