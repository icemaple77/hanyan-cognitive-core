"""Scanner configuration loaded from ``HCC_SCANNER_*`` environment variables.

All runtime knobs — the directories to scan, the file patterns, the scan
interval, whether we run in dry-run mode and the target Memory API URL — are
sourced from the environment so the scanner can run unchanged as a local
process or inside a container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _default_state_db() -> Path:
    """Return the default SQLite state database path (``~/.hcc/scanner_state.db``)."""
    return Path.home() / ".hcc" / "scanner_state.db"


class ScannerSettings(BaseSettings):
    """Configuration for the HCC file scanner.

    Every attribute maps to an environment variable prefixed with
    ``HCC_SCANNER_`` (for example ``HCC_SCANNER_DIRS``). List-valued options
    accept either a JSON array or a plain comma-separated string.
    """

    model_config = SettingsConfigDict(
        env_prefix="HCC_SCANNER_",
        env_file=".env",
        extra="ignore",
    )

    dirs: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Directories to scan recursively for markdown files.",
    )
    patterns: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*.md"],
        description="Glob patterns matched against file names.",
    )
    exclude: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["node_modules", ".git", "__pycache__"],
        description="Directory names to skip while walking the tree.",
    )
    interval: int = Field(
        default=3600,
        ge=1,
        description="Seconds to wait between scan passes when running as a loop.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, log what would be absorbed without calling the API.",
    )
    once: bool = Field(
        default=False,
        description="If true, perform a single scan pass and exit.",
    )
    user_id: str = Field(
        default="scanner",
        description="user_id attached to every stored Memory entry.",
    )
    source: str = Field(
        default="scanner",
        description="source label attached to every stored Memory entry.",
    )
    request_timeout: float = Field(
        default=30.0,
        gt=0,
        description="HTTP request timeout (seconds) for Memory API calls.",
    )
    generate_qmd: bool = Field(
        default=False,
        description=(
            "If true, regenerate the QMD knowledge tree after a scan and record "
            "which QMD file each absorbed document produced "
            "(HCC_SCANNER_GENERATE_QMD)."
        ),
    )
    sync: bool = Field(
        default=False,
        description=(
            "If true, run a full bidirectional PostgreSQL<->QMD sync after each "
            "scan pass (HCC_SCANNER_SYNC)."
        ),
    )
    watch: bool = Field(
        default=False,
        description=(
            "If true, continuously watch the scan dirs for changes using "
            "watchdog instead of polling (HCC_SCANNER_WATCH)."
        ),
    )
    state_db: Path = Field(
        default_factory=_default_state_db,
        description="Path to the SQLite database used for state tracking.",
    )
    # Accept both HCC_SCANNER_MEMORY_API_URL and the plan's HCC_MEMORY_API_URL.
    memory_api_url: str = Field(
        default="http://localhost:8000/api/v1",
        validation_alias=AliasChoices(
            "HCC_SCANNER_MEMORY_API_URL", "HCC_MEMORY_API_URL"
        ),
        description="Base URL of the HCC Memory API.",
    )

    @field_validator("dirs", "patterns", "exclude", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow comma-separated strings for list-valued settings.

        Pydantic natively parses JSON arrays; here we also accept a plain
        ``"a,b,c"`` string, splitting on commas and trimming whitespace.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json

                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def store_url(self) -> str:
        """Return the fully-qualified URL of the ``/memory/store`` endpoint."""
        return f"{self.memory_api_url.rstrip('/')}/memory/store"

    def scan_dirs(self) -> list[Path]:
        """Return configured scan directories as resolved :class:`Path` objects."""
        return [Path(d).expanduser() for d in self.dirs]


def load_settings() -> ScannerSettings:
    """Instantiate :class:`ScannerSettings` from the current environment."""
    return ScannerSettings()
