"""End-to-end: real HTTP server, claude.ai-style OAuth (DCR + PKCE + consent), MCP tool calls.

Garmin is replaced by an in-memory fake, so no account is needed.
"""

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn

from garmin_mcp.config import Settings
from garmin_mcp.oauth import OwnerOAuthProvider
from garmin_mcp.server import build_server

PASSWORD = "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class FakeGarmin:
    full_name, display_name = "Test Rider", "rider"

    def __init__(self):
        self.power = [{"sport": "CYCLING", "functionalThresholdPower": 250, **{
            f"zone{i}Floor": f for i, f in enumerate([0, 138, 188, 228, 263, 300, 375], 1)}}]
        self.hr = [{"sport": "DEFAULT", "trainingMethod": "HR_MAX", "maxHeartRateUsed": 190,
                    "restingHeartRateUsed": 50, "lactateThresholdHeartRateUsed": 170,
                    **{f"zone{i}Floor": f for i, f in enumerate([95, 114, 133, 152, 171], 1)}}]
        self.workouts, self.schedule, self.next_id = {}, {}, 1000

    def get_user_profile(self):
        return {"userData": {"weight": 72000, "measurementSystem": "metric"}}

    def get_power_zones(self):
        return json.loads(json.dumps(self.power))

    def update_power_zones(self, zones):
        self.power = zones

    def get_heart_rate_zones_v2(self):
        return json.loads(json.dumps(self.hr))

    def update_heart_rate_zones(self, zones):
        self.hr = zones

    def get_cycling_ftp(self):
        return {"functionalThresholdPower": self.power[0]["functionalThresholdPower"]}

    def upload_workout(self, payload):
        self.next_id += 1
        self.workouts[self.next_id] = payload | {"workoutId": self.next_id}
        return self.workouts[self.next_id]

    def update_workout(self, wid, payload):
        self.workouts[wid] = payload | {"workoutId": wid}

    def get_workout_by_id(self, wid):
        return self.workouts[wid]

    def get_workouts(self, start, limit, **kw):
        return list(self.workouts.values())[start:start + limit]

    def delete_workout(self, wid):
        del self.workouts[wid]

    def schedule_workout(self, wid, d):
        self.next_id += 1
        self.schedule[self.next_id] = (wid, d)
        return {"workoutScheduleId": self.next_id}

    def get_scheduled_workouts(self, y, m):
        return {"calendarItems": [
            {"id": sid, "itemType": "workout", "date": d, "workoutId": wid,
             "title": self.workouts[wid]["workoutName"], "sportTypeKey": "cycling"}
            for sid, (wid, d) in self.schedule.items() if d.startswith(f"{y}-{m:02d}")]}

    def reschedule_workout(self, sid, d):
        self.schedule[sid] = (self.schedule[sid][0], d)

    def unschedule_workout(self, sid):
        del self.schedule[sid]


class FakeSession:
    def __init__(self):
        self.fake = FakeGarmin()

    def call(self, fn):
        return fn(self.fake)

    async def acall(self, fn):
        return fn(self.fake)

    def status(self):
        return {"connected": True}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(public_url=base, owner_password=PASSWORD, data_dir=Path(tmp_path_factory.mktemp("data")),
                        allowed_redirect_prefixes=("https://claude.ai/",))
    session = FakeSession()
    mcp = build_server(session, OwnerOAuthProvider(settings), base)  # type: ignore[arg-type]
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield base, session.fake
    srv.should_exit = True
    t.join(5)


def _oauth(base: str) -> str:
    c = httpx.Client(base_url=base, follow_redirects=False)
    # 1. unauthenticated MCP call -> 401 pointing at protected resource metadata
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    # 2. a non-Claude redirect URI is refused
    bad = c.post(meta["registration_endpoint"], json={"redirect_uris": ["https://evil.example/cb"],
                                                      "token_endpoint_auth_method": "none"})
    assert bad.status_code == 400
    reg = c.post(meta["registration_endpoint"], json={
        "client_name": "Claude", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"})
    assert r.status_code == 302
    consent = r.headers["location"]
    rid = parse_qs(urlparse(consent).query)["rid"][0]
    page = c.get(consent)
    assert page.status_code == 200 and "Autoriser" in page.text
    # 3. wrong password, then right one
    r = c.post("/oauth/consent", data={"rid": rid, "password": "nope", "action": "allow"})
    assert r.status_code == 200 and "incorrect" in r.text
    r = c.post("/oauth/consent", data={"rid": rid, "password": PASSWORD, "action": "allow"})
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    tok = c.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": reg["client_id"], "code_verifier": verifier}).json()
    assert tok["refresh_token"]
    # 4. refresh rotates tokens; old refresh token is single-use
    tok2 = c.post(meta["token_endpoint"], data={"grant_type": "refresh_token", "client_id": reg["client_id"],
                                                "refresh_token": tok["refresh_token"]}).json()
    assert tok2["access_token"] != tok["access_token"]
    reuse = c.post(meta["token_endpoint"], data={"grant_type": "refresh_token", "client_id": reg["client_id"],
                                                 "refresh_token": tok["refresh_token"]})
    assert reuse.status_code == 400
    return tok2["access_token"]


class MCP:
    def __init__(self, base, token):
        self.c = httpx.Client(base_url=base, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        self.i = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
        self.version = init["protocolVersion"]

    def rpc(self, method, params=None):
        self.i += 1
        r = self.c.post("/mcp", json={"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}},
                        headers={"MCP-Protocol-Version": getattr(self, "version", "2025-06-18")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "error" not in body, body
        return body["result"]

    def tool(self, _tool, **args):
        res = self.rpc("tools/call", {"name": _tool, "arguments": args})
        assert not res.get("isError"), res
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        return json.loads(res["content"][0]["text"])


def test_full_flow(server):
    base, fake = server
    m = MCP(base, _oauth(base))
    names = {t["name"] for t in m.rpc("tools/list")["tools"]}
    assert {"get_athlete_profile", "set_ftp", "create_cycling_workout", "schedule_workout",
            "set_heart_rate_zones", "list_scheduled_workouts"} <= names

    prof = m.tool("get_athlete_profile")
    assert prof["cycling_ftp_w"] == 250 and prof["user"]["weight_kg"] == 72.0

    w = m.tool("create_cycling_workout", name="SST 3x10", schedule_date="2026-11-20", steps=[
        {"type": "warmup", "duration_s": 600, "target": {"kind": "power_pct_ftp", "low": 50, "high": 60}},
        {"type": "repeat", "iterations": 3, "steps": [
            {"type": "interval", "duration_s": 600, "target": {"kind": "power_pct_ftp", "low": 88, "high": 92}},
            {"type": "recovery", "duration_s": 300}]},
        {"type": "cooldown", "duration_s": 600}])
    assert w["ftp_used_w"] == 250 and w["estimated_duration_min"] == 65
    assert w["steps"][0] == "- warmup 10:00 | power 125-150 W"
    sid = w["scheduled"]["scheduled_workout_id"]

    cal = m.tool("list_scheduled_workouts", start="2026-11-15", end="2026-12-31")
    assert [(x["date"], x["name"]) for x in cal] == [("2026-11-20", "SST 3x10")]
    m.tool("reschedule_workout", scheduled_workout_id=sid, new_date="2026-12-02")
    assert m.tool("list_scheduled_workouts", start="2026-12-01", end="2026-12-03")[0]["date"] == "2026-12-02"

    r = m.tool("set_ftp", ftp_watts=275)
    assert r["after"]["ftp_w"] == 275 and fake.power[0]["zone2Floor"] == 152

    r = m.tool("set_heart_rate_zones", max_hr=195)
    assert r["after"]["max_hr"] == 195 and fake.hr[0]["zone5Floor"] == 176

    # error surfaces as tool error, not a crash
    bad = m.rpc("tools/call", {"name": "create_cycling_workout",
                               "arguments": {"name": "x", "steps": [{"type": "interval", "duration_s": 60,
                                                                     "target": {"kind": "power_zone", "zone": 12}}]}})
    assert bad["isError"]

    m.tool("schedule_workout", workout_id=w["workout_id"], workout_date="2026-11-25")
    m.tool("delete_workout", workout_id=w["workout_id"])
    assert m.tool("list_workouts") == []
