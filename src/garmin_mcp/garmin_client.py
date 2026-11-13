"""Thread-safe, lazily authenticated Garmin Connect client.

The server never sees your Garmin password: it only loads the token file
created once with ``garmin-mcp-login``. The library refreshes and re-saves
those tokens on its own; a keepalive call every few hours keeps the refresh
chain alive, and a new token file (after a re-login) is picked up without a
restart.

Every failure is turned into a ``ToolError`` with an actionable message, so
the LLM sees *why* a call failed instead of a generic "Error executing tool".
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

logger = logging.getLogger(__name__)
T = TypeVar("T")

RELOGIN = "On the server, open the container terminal in Dokploy and run `garmin-mcp-login --force`."


class GarminNotLoggedIn(RuntimeError):
    pass


def _status_code(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b([45]\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def friendly_error(exc: BaseException) -> ToolError:
    """Map any exception raised while talking to Garmin to a clear ToolError."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, GarminNotLoggedIn):
        return ToolError(f"Garmin is not connected yet. {RELOGIN.replace(' --force', '')}")
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return ToolError("Garmin is rate-limiting requests (HTTP 429). Wait a few minutes, then retry.")
    if isinstance(exc, GarminConnectAuthenticationError):
        return ToolError(f"The Garmin session expired or was revoked ({exc}). {RELOGIN}")
    if isinstance(exc, GarminConnectNotFoundError):
        return ToolError(f"Not found on Garmin Connect: {exc}")
    if isinstance(exc, ValidationError):
        return ToolError(f"Invalid input: {exc}")
    if isinstance(exc, (ValueError, TypeError, KeyError)) and not isinstance(exc, GarminConnectConnectionError):
        return ToolError(f"Invalid request: {exc}")
    code = _status_code(exc)
    if code in (401, 403):
        return ToolError(f"Garmin refused access (HTTP {code}). {RELOGIN}")
    if code == 404:
        return ToolError(f"Not found on Garmin Connect (HTTP 404): {exc}")
    if code == 429:
        return ToolError("Garmin is rate-limiting requests (HTTP 429). Wait a few minutes, then retry.")
    if code and 400 <= code < 500:
        return ToolError(f"Garmin rejected the request (HTTP {code}): {exc}")
    if code and code >= 500:
        return ToolError(f"Garmin Connect is having trouble (HTTP {code}). Retry in a moment.")
    if isinstance(exc, GarminConnectConnectionError):
        return ToolError(f"Could not reach Garmin Connect: {exc}")
    return ToolError(f"Unexpected error ({type(exc).__name__}): {exc}")


class GarminSession:
    def __init__(self, tokens_path: Path) -> None:
        self._tokens_path = tokens_path
        self._api: Garmin | None = None
        self._tokens_mtime: float | None = None
        self._lock = threading.RLock()
        self.last_ok: float | None = None

    def _mtime(self) -> float | None:
        try:
            return self._tokens_path.stat().st_mtime
        except OSError:
            return None

    def _connect(self) -> Garmin:
        if not self._tokens_path.exists():
            raise GarminNotLoggedIn("no token file")
        api = Garmin()
        api.login(tokenstore=str(self._tokens_path))
        self._tokens_mtime = self._mtime()
        logger.info("Connected to Garmin Connect as %s", api.display_name)
        return api

    def api(self) -> Garmin:
        with self._lock:
            if self._api is not None and self._mtime() != self._tokens_mtime:
                # garmin-mcp-login wrote new tokens (or the library refreshed them):
                # reconnecting is cheap and picks up a manual re-login without a restart.
                self._api = None
            if self._api is None:
                self._api = self._connect()
            return self._api

    def _run(self, fn: Callable[[Garmin], T]) -> T:
        with self._lock:
            try:
                result = fn(self.api())
            except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
                code = _status_code(exc)
                if not isinstance(exc, GarminConnectAuthenticationError) and code not in (401, 403):
                    raise
                logger.warning("Garmin auth error, reconnecting from the token file: %s", exc)
                self._api = None
                result = fn(self.api())
            self.last_ok = time.time()
            return result

    def call(self, fn: Callable[[Garmin], T]) -> T:
        try:
            return self._run(fn)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a clear ToolError
            err = friendly_error(exc)
            if str(err).startswith("Unexpected error"):
                logger.exception("Unexpected Garmin error")
            else:
                logger.info("Garmin call failed: %s", err)
            raise err from exc

    async def acall(self, fn: Callable[[Garmin], T]) -> T:
        return await anyio.to_thread.run_sync(self.call, fn)

    def status(self) -> dict[str, Any]:
        try:
            api = self.api()
            api.get_user_profile()
            self.last_ok = time.time()
            return {"connected": True, "display_name": api.display_name, "full_name": api.full_name}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as status
            return {"connected": False, "error": str(friendly_error(exc))}

    def start_keepalive(self, every_s: float = 6 * 3600) -> None:
        """Touch the API periodically so tokens are refreshed before they lapse."""

        def loop() -> None:
            while True:
                time.sleep(every_s)
                st = self.status()
                if st["connected"]:
                    logger.info("Garmin keepalive OK")
                else:
                    logger.warning("Garmin keepalive failed: %s", st.get("error"))

        threading.Thread(target=loop, name="garmin-keepalive", daemon=True).start()
