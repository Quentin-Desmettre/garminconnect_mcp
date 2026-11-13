"""Thread-safe, lazily authenticated Garmin Connect client.

The server never sees your Garmin password: it only loads the token file
created once with ``garmin-mcp-login``. The library refreshes and re-saves
those tokens on its own.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class GarminNotLoggedIn(RuntimeError):
    pass


class GarminSession:
    def __init__(self, tokens_path: Path) -> None:
        self._tokens_path = tokens_path
        self._api: Garmin | None = None
        self._lock = threading.RLock()

    def _connect(self) -> Garmin:
        if not self._tokens_path.exists():
            raise GarminNotLoggedIn(
                "Garmin is not connected yet. On the server, run "
                "`garmin-mcp-login` once to create the Garmin token file."
            )
        api = Garmin()
        api.login(tokenstore=str(self._tokens_path))
        logger.info("Connected to Garmin Connect as %s", api.display_name)
        return api

    def api(self) -> Garmin:
        with self._lock:
            if self._api is None:
                self._api = self._connect()
            return self._api

    def call(self, fn: Callable[[Garmin], T]) -> T:
        """Run ``fn(api)``; on an auth failure reconnect once from the token file."""
        with self._lock:
            try:
                return fn(self.api())
            except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
                if "401" not in str(exc) and not isinstance(exc, GarminConnectAuthenticationError):
                    raise
                logger.warning("Garmin auth error, reconnecting: %s", exc)
                self._api = None
                return fn(self.api())

    async def acall(self, fn: Callable[[Garmin], T]) -> T:
        return await anyio.to_thread.run_sync(self.call, fn)

    def status(self) -> dict[str, Any]:
        try:
            api = self.api()
            return {"connected": True, "display_name": api.display_name, "full_name": api.full_name}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as status
            return {"connected": False, "error": str(exc)}
