"""Server-side confirmation for destructive or account-wide changes.

A guarded tool called without ``confirm_token`` does nothing: it returns a
preview and a one-time token bound to the exact arguments. The LLM shows the
preview to the user and, once they agree, calls the tool again with the same
arguments plus the token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

TTL_S = 15 * 60


def _digest(action: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"a": action, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class ConfirmStore:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def request(self, action: str, args: dict[str, Any], preview: Any) -> dict[str, Any]:
        token = secrets.token_urlsafe(9)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
            self._pending[token] = (now + TTL_S, _digest(action, args))
        return {
            "status": "confirmation_required",
            "action": action,
            "preview": preview,
            "confirm_token": token,
            "next_step": "Nothing was changed. Show this preview to the user. If they agree, call the same tool "
            "again with exactly the same arguments plus this confirm_token (valid 15 minutes, single use).",
        }

    def check(self, token: str, action: str, args: dict[str, Any]) -> None:
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None or entry[0] < time.time():
            raise ToolError("confirm_token is unknown or expired: call the tool again without it to get a new preview.")
        if entry[1] != _digest(action, args):
            raise ToolError("confirm_token does not match these arguments: call again without it to get a new preview.")
