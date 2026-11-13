"""Minimal single-user OAuth 2.1 authorization server for claude.ai connectors.

Flow: claude.ai registers itself (dynamic client registration), sends you to
/authorize, which redirects to a consent page asking for OWNER_PASSWORD. On
success an authorization code is issued (PKCE is enforced by the MCP SDK).

Clients and tokens are persisted in a JSON file so a container restart does
not disconnect claude.ai. Tokens are stored as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

logger = logging.getLogger(__name__)

SCOPE = "garmin"
PENDING_TTL = 600
CODE_TTL = 300
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 900


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Store:
    """Tiny JSON-file store (clients, access tokens, refresh tokens)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {"clients": {}, "access": {}, "refresh": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except (OSError, ValueError):
                logger.exception("Could not read OAuth store %s, starting empty", path)

    def save(self) -> None:
        with self._lock:
            now = time.time()
            for kind in ("access", "refresh"):
                self.data[kind] = {
                    k: v for k, v in self.data[kind].items() if not v.get("expires_at") or v["expires_at"] > now
                }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            tmp.replace(self.path)


class OwnerOAuthProvider:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.store = _Store(settings.oauth_store_path)
        self.pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failed: list[float] = []

    # ---- clients -----------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not any(str(uri).startswith(p) for p in self.s.allowed_redirect_prefixes):
                raise RegistrationError(
                    "invalid_redirect_uri", f"redirect_uri {uri} is not allowed by this server"
                )
        self.store.data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self.store.save()
        logger.info("Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)

    # ---- authorization -----------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[0] > now}
        if len(self.pending) > 50:
            raise AuthorizeError("temporarily_unavailable", "too many pending authorizations")
        rid = secrets.token_urlsafe(24)
        self.pending[rid] = (now + PENDING_TTL, client.client_id, params)
        return f"{self.s.public_url}/oauth/consent?rid={rid}"

    def _locked(self) -> bool:
        now = time.time()
        self.failed = [t for t in self.failed if t > now - LOCKOUT_SECONDS]
        return len(self.failed) >= MAX_FAILED_LOGINS

    async def consent_page(self, request: Request) -> Response:
        rid = request.query_params.get("rid") if request.method == "GET" else (await request.form()).get("rid")
        rid = str(rid or "")
        entry = self.pending.get(rid)
        if not entry or entry[0] < time.time():
            return HTMLResponse(_page("Lien expiré", "<p>Relance la connexion depuis Claude.</p>"), 400)
        _, client_id, params = entry
        client = await self.get_client(client_id)
        client_name = html.escape((client.client_name if client else None) or client_id)

        error = ""
        if request.method == "POST":
            form = await request.form()
            if self._locked():
                error = "Trop d'essais. Réessaie dans 15 minutes."
            elif str(form.get("action")) == "deny":
                self.pending.pop(rid, None)
                return RedirectResponse(
                    construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state),
                    302,
                )
            elif hmac.compare_digest(str(form.get("password", "")).encode(), self.s.owner_password.encode()):
                self.pending.pop(rid, None)
                code = secrets.token_urlsafe(32)
                self.codes[code] = AuthorizationCode(
                    code=code,
                    scopes=params.scopes or [SCOPE],
                    expires_at=time.time() + CODE_TTL,
                    client_id=client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource,
                    subject="owner",
                )
                logger.info("Authorization granted to client %s", client_id)
                return RedirectResponse(
                    construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302
                )
            else:
                self.failed.append(time.time())
                logger.warning("Wrong owner password on consent page")
                error = "Mot de passe incorrect."

        body = f"""
        <p><b>{client_name}</b> demande l'accès à ton compte Garmin Connect
        (entraînements, calendrier, FTP et zones).</p>
        {"<p class='err'>" + html.escape(error) + "</p>" if error else ""}
        <form method="post">
          <input type="hidden" name="rid" value="{html.escape(rid)}">
          <label>Mot de passe du serveur
            <input type="password" name="password" autocomplete="current-password" autofocus>
          </label>
          <div class="row">
            <button name="action" value="allow" type="submit">Autoriser</button>
            <button name="action" value="deny" type="submit" class="secondary">Refuser</button>
          </div>
        </form>"""
        return HTMLResponse(_page("Connexion Garmin MCP", body))

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self.store.data["access"][_h(access)] = common | {"expires_at": now + self.s.access_token_ttl}
        self.store.data["refresh"][_h(refresh)] = common | {"expires_at": now + self.s.refresh_token_ttl}
        self.store.save()
        return OAuthToken(
            access_token=access,
            expires_in=self.s.access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        raw = self.store.data["refresh"].get(_h(refresh_token))
        if not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, subject="owner", **raw)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if self.store.data["refresh"].pop(_h(refresh_token.token), None) is None:
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.store.data["access"].get(_h(token))
        if not raw or raw["expires_at"] < time.time():
            return None
        return AccessToken(token=token, subject="owner", **raw)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _h(token.token)
        self.store.data["access"].pop(h, None)
        self.store.data["refresh"].pop(h, None)
        self.store.save()


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f5f5f4;color:#1c1917;display:flex;
justify-content:center;padding:48px 16px;margin:0}}
main{{background:#fff;border-radius:12px;padding:28px;max-width:420px;width:100%;
box-shadow:0 1px 3px rgba(0,0,0,.1)}}
h1{{font-size:1.25rem;margin-top:0}} label{{display:block;margin:16px 0}}
input[type=password]{{display:block;width:100%;box-sizing:border-box;margin-top:6px;padding:10px;
border:1px solid #d6d3d1;border-radius:8px;font-size:1rem}}
.row{{display:flex;gap:8px}} button{{flex:1;padding:10px;border:0;border-radius:8px;font-size:1rem;
background:#0369a1;color:#fff;cursor:pointer}} button.secondary{{background:#e7e5e4;color:#1c1917}}
.err{{color:#b91c1c}}
@media (prefers-color-scheme:dark){{body{{background:#1c1917;color:#f5f5f4}}main{{background:#292524}}
input[type=password]{{background:#1c1917;color:#f5f5f4;border-color:#57534e}}
button.secondary{{background:#44403c;color:#f5f5f4}}}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
