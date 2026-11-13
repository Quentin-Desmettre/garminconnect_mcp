"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value or ""


@dataclass(frozen=True)
class Settings:
    # Public base URL of the server, as seen by claude.ai (no trailing slash),
    # e.g. https://garmin-mcp.example.com
    public_url: str
    # Password you type on the consent page when connecting claude.ai.
    owner_password: str
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8000
    # Redirect URIs a client may register (prefix match). Default: Claude only.
    allowed_redirect_prefixes: tuple[str, ...] = field(default_factory=tuple)
    access_token_ttl: int = 24 * 3600
    refresh_token_ttl: int = 90 * 24 * 3600

    @property
    def garmin_tokens_path(self) -> Path:
        return self.data_dir / "garmin_tokens.json"

    @property
    def oauth_store_path(self) -> Path:
        return self.data_dir / "oauth_store.json"


def load_settings() -> Settings:
    public_url = _env("PUBLIC_URL", required=True).rstrip("/")
    owner_password = _env("OWNER_PASSWORD", required=True)
    if len(owner_password) < 12:
        raise SystemExit("OWNER_PASSWORD must be at least 12 characters long")
    prefixes = _env(
        "ALLOWED_REDIRECT_PREFIXES",
        "https://claude.ai/,https://claude.com/,http://localhost:,http://127.0.0.1:",
    )
    return Settings(
        public_url=public_url,
        owner_password=owner_password,
        data_dir=Path(_env("DATA_DIR", "/data")),
        host=_env("HOST", "0.0.0.0"),
        port=int(_env("PORT", "8000")),
        allowed_redirect_prefixes=tuple(p.strip() for p in prefixes.split(",") if p.strip()),
    )
