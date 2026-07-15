# Phase 3: File Scanner

Scan filesystem directories for markdown files and absorb them into HCC Memory.

## Structure

```
scanner/
├── __init__.py
├── watcher.py       — Directory scanner (watchdog)
├── parser.py        — Markdown parser
├── absorber.py      — Absorb parsed content into Memory
└── config.py        — Scanner configuration
```

## Features

1. **Directory scanning** — Scan configured directories recursively for `.md` files
2. **File hashing** — Track files by SHA256 to avoid re-processing
3. **Markdown parsing** — Extract title, content, tags, metadata from markdown files
4. **Memory absorption** — Parse markdown → store as Memory entry via MCP tools
5. **State tracking** — SQLite db to track which files have been processed
6. **Configurable** — All paths, exclusions, and patterns via env vars (HCC_SCANNER_*)

## Config options (env vars)

- `HCC_SCANNER_DIRS` — Comma-separated directories to scan
- `HCC_SCANNER_PATTERNS` — File glob patterns (default: *.md)
- `HCC_SCANNER_EXCLUDE` — Directories to exclude (default: node_modules,.git,__pycache__)
- `HCC_SCANNER_INTERVAL` — Scan interval in seconds (default: 3600 = 1 hour)
- `HCC_SCANNER_DRY_RUN` — If true, log what would be done without storing (default: false)
- `HCC_MEMORY_API_URL` — HCC Memory API base URL (default: http://localhost:8000/api/v1)

## Integration

- Scanner can run as a standalone process or Docker container
- Scanner uses the Memory API (HTTP) to store memories, not direct DB access
- State file stored at `~/.hcc/scanner_state.db`
