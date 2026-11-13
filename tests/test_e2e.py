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
from garmin_mcp.garmin_client import GarminNotLoggedIn, GarminSession
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

    # ---- scheduled entry, copy
    def get_scheduled_workout_by_id(self, sid):
        wid, d = self.schedule[sid]
        return {"calendarDate": d, "workout": {"workoutId": wid, "workoutName": self.workouts[wid]["workoutName"]}}

    def copy_workout(self, wid, name):
        return self.upload_workout(self.workouts[wid] | {"workoutName": name})

    # ---- activities (shapes taken from a real Edge 830 account)
    activities = {
        111: {"activityId": 111, "activityName": "Lille - PMA 2x3 min", "startTimeLocal": "2026-11-10 12:48:31",
              "activityType": {"typeKey": "road_biking"}, "duration": 1800.0, "distance": 15000.0, "avgPower": 160,
              "normPower": 180, "averageHR": 150, "maxHR": 175, "trainingStressScore": 50.0, "intensityFactor": 0.9,
              "workoutId": None},
    }

    def get_activities_by_date(self, start, end=None, *a):
        return [a for a in self.activities.values() if start <= a["startTimeLocal"][:10] <= (end or "9999")]

    def get_activity(self, aid):
        a = self.activities[aid]
        return {"activityId": aid, "activityName": a["activityName"], "description": a.get("description"),
                "activityTypeDTO": a["activityType"],
                "metadataDTO": {"associatedWorkoutId": a.get("workoutId")},
                "summaryDTO": {"startTimeLocal": a["startTimeLocal"], "duration": a["duration"],
                               "averagePower": a.get("avgPower"), "normalizedPower": a.get("normPower"),
                               "trainingStressScore": a.get("trainingStressScore"), "functionalThresholdPower": 250}}

    def get_activity_laps(self, aid):
        return {"lapDTOs": self.laps.get(aid, [])}

    laps: dict = {}

    def get_activity_power_in_timezones(self, aid):
        return [{"zoneNumber": 1, "secsInZone": 600, "zoneLowBoundary": 0}]

    def get_activity_hr_in_timezones(self, aid):
        return []

    def get_activity_types(self):
        return [{"typeId": 25, "typeKey": "indoor_cycling", "parentTypeId": 2},
                {"typeId": 10, "typeKey": "road_biking", "parentTypeId": 2},
                {"typeId": 13, "typeKey": "strength_training", "parentTypeId": 29}]

    def update_activity(self, aid, fields):
        a = self.activities[aid]
        if "activityName" in fields:
            a["activityName"] = fields["activityName"]
        if "description" in fields:
            a["description"] = fields["description"]
        if "activityTypeDTO" in fields:
            a["activityType"] = {"typeKey": fields["activityTypeDTO"]["typeKey"]}

    # ---- strength
    sets: dict = {}

    def get_user_timezone(self):
        return "Europe/Paris"

    def create_manual_activity(self, start, tz, type_key, dist, minutes, name):
        aid = max(self.activities) + 1
        self.activities[aid] = {"activityId": aid, "activityName": name, "startTimeLocal": start.replace("T", " ")[:19],
                                "activityType": {"typeKey": type_key}, "duration": minutes * 60.0}
        return {"activityId": aid}

    def set_activity_exercise_sets(self, aid, payload):
        assert payload["activityId"] == aid
        self.sets[aid] = payload

    def get_activity_exercise_sets(self, aid):
        return self.sets.get(aid, {"exerciseSets": []})

    def set_activity_description(self, aid, text):
        self.activities[int(aid)]["description"] = text

    # ---- wellness: an Edge-only account has none of these
    def get_sleep_data(self, d):
        return {"dailySleepDTO": {"sleepTimeSeconds": None}}

    def get_hrv_data(self, d):
        return {}

    def get_rhr_daily(self, a, b):
        return []

    def get_body_battery(self, a, b=None):
        return []

    def get_training_readiness(self, d):
        return []

    def get_training_status(self, d):
        return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
            "trainingStatusFeedbackPhrase": "PRODUCTIVE", "weeklyTrainingLoad": 500,
            "loadTunnelMin": 300, "loadTunnelMax": 650}}}}

    # ---- body
    def get_body_composition(self, a, b):
        return {"dateWeightList": [{"calendarDate": "2026-11-01", "weight": 73000.0, "sourceType": "MANUAL"}]}

    def get_latest_weight(self, d):
        return {"weight": 73000.0}

    def add_weigh_in(self, kg, unit, ts):
        self.weighins.append((kg, unit, ts))

    weighins: list = []


class FakeSession(GarminSession):
    """Real GarminSession (error mapping included) whose API is the in-memory fake."""

    def __init__(self):
        super().__init__(Path("/nonexistent/tokens.json"))
        self.fake = FakeGarmin()
        self.logged_in = True

    def api(self):
        if not self.logged_in:
            raise GarminNotLoggedIn("no token file")
        return self.fake


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
    yield base, session
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
        return self._decode(res)

    def tool_error(self, _tool, **args):
        res = self.rpc("tools/call", {"name": _tool, "arguments": args})
        assert res.get("isError"), res
        return res["content"][0]["text"]

    def confirmed(self, _tool, **args):
        preview = self.tool(_tool, **args)
        assert preview["status"] == "confirmation_required", preview
        return self.tool(_tool, **args, confirm_token=preview["confirm_token"])

    @staticmethod
    def _decode(res):
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        return json.loads(res["content"][0]["text"])


def test_full_flow(server):
    base, session = server
    fake = session.fake
    m = MCP(base, _oauth(base))
    names = {t["name"] for t in m.rpc("tools/list")["tools"]}
    assert len(names) == 28 and {"get_activity", "get_recovery_status", "log_strength_session"} <= names

    prof = m.tool("get_athlete_profile")
    assert prof["cycling_ftp_w"] == 250 and prof["user"]["weight_kg"] == 72.0 and prof["ftp_w_per_kg"] == 3.47

    w = m.tool("create_cycling_workout", name="SST 3x10", schedule_date="2026-11-20", steps=[
        {"type": "warmup", "duration_s": 600, "target": {"kind": "power_pct_ftp", "low": 50, "high": 60}},
        {"type": "repeat", "iterations": 3, "steps": [
            {"type": "interval", "duration_s": 600, "target": {"kind": "power_pct_ftp", "low": 88, "high": 92}},
            {"type": "recovery", "duration_s": 300}]},
        {"type": "cooldown", "duration_s": 600}])
    assert w["ftp_used_w"] == 250 and w["estimated_duration_min"] == 65
    assert w["steps"][0] == "- warmup 10:00 | power 125-150 W"
    sid = w["scheduled"][0]["scheduled_workout_id"]

    cal = m.tool("list_scheduled_workouts", start="2026-11-15", end="2026-12-31")
    assert [(x["date"], x["name"]) for x in cal] == [("2026-11-20", "SST 3x10")]
    m.tool("reschedule_workout", scheduled_workout_id=sid, new_date="2026-12-02")
    assert m.tool("list_scheduled_workouts", start="2026-12-01", end="2026-12-03")[0]["date"] == "2026-12-02"

    # guarded change: the first call changes nothing
    preview = m.tool("set_ftp", ftp_watts=275)
    assert preview["status"] == "confirmation_required" and preview["preview"]["after"]["ftp_w"] == 275
    assert fake.power[0]["functionalThresholdPower"] == 250
    assert "does not match" in m.tool_error("set_ftp", ftp_watts=280, confirm_token=preview["confirm_token"])
    preview = m.tool("set_ftp", ftp_watts=275)
    r = m.tool("set_ftp", ftp_watts=275, confirm_token=preview["confirm_token"])
    assert r["status"] == "applied" and fake.power[0]["zone2Floor"] == 152
    assert "unknown or expired" in m.tool_error("set_ftp", ftp_watts=275, confirm_token=preview["confirm_token"])

    r = m.confirmed("set_heart_rate_zones", max_hr=195)
    assert r["after"]["max_hr"] == 195 and fake.hr[0]["zone5Floor"] == 176

    # after the FTP test: rescale watt targets of planned workouts (250 -> 275)
    r = m.confirmed("update_workouts_for_ftp", start="2026-11-01", end="2026-12-31", old_ftp_w=250, new_ftp_w=275)
    assert r["updated"] == ["SST 3x10"]
    assert m.tool("get_workout", workout_id=w["workout_id"])["steps"][0] == "- warmup 10:00 | power 138-165 W"

    # batch create + duplicate + batch schedule
    b = m.tool("create_cycling_workouts", workouts=[
        {"name": "Z2 1h", "steps": [{"type": "interval", "duration_s": 3600,
                                     "target": {"kind": "power_zone", "zone": 2}}], "schedule_dates": ["2026-11-21"]},
        {"name": "broken", "steps": [{"type": "interval", "duration_s": 60}], "schedule_dates": ["21/11"]}])
    assert b["created"] == 1 and b["failed"] == 1 and "YYYY-MM-DD" in b["results"][1]["error"]
    dup = m.tool("duplicate_workout", workout_id=b["results"][0]["workout_id"], new_name="Z2 1h30",
                 schedule_dates=["2026-11-23"])
    assert dup["scheduled"][0]["date"] == "2026-11-23"
    s2 = m.tool("schedule_workouts", items=[{"workout_id": dup["workout_id"], "date": "2026-11-30"}])
    assert s2["scheduled"] == 1

    # clear validation errors
    err = m.tool_error("create_cycling_workout", name="x",
                       steps=[{"type": "interval", "duration_s": 60, "target": {"kind": "power_zone", "zone": 12}}])
    assert "zone" in err
    assert "YYYY-MM-DD" in m.tool_error("list_scheduled_workouts", start="13/11/2026")

    # activities: fix the type of a session recorded as an outdoor ride
    acts = m.tool("list_activities", start="2026-11-01", end="2026-11-13", activity_type="cycling")
    assert acts[0]["type"] == "road_biking" and acts[0]["tss"] == 50.0
    u = m.tool("update_activity", activity_id=111, activity_type="indoor_cycling", name="PMA 2x3 min")
    assert u["before"]["type"] == "road_biking" and u["after"] == {"name": "PMA 2x3 min", "type": "indoor_cycling",
                                                                    "description": None}
    assert "unknown activity type" in m.tool_error("update_activity", activity_id=111, activity_type="spaceship")

    # planned vs actual on a structured activity
    fake.activities[111]["workoutId"] = w["workout_id"]
    fake.laps[111] = [
        {"lapIndex": 1, "wktStepIndex": 0, "duration": 600, "averagePower": 150, "averageHR": 120},
        {"lapIndex": 2, "wktStepIndex": 1, "duration": 600, "averagePower": 245, "averageHR": 160},
        {"lapIndex": 3, "wktStepIndex": 2, "duration": 300, "averagePower": 120, "averageHR": 140},
        {"lapIndex": 4, "wktStepIndex": 1, "duration": 600, "averagePower": 230, "averageHR": 165},
    ]
    a = m.tool("get_activity", activity_id=111)
    assert a["workout"]["name"] == "SST 3x10" and a["time_in_power_zones"][0]["minutes"] == 10.0
    iv = [s for s in a["steps"] if s["step"] == "interval"]
    assert [s["rep"] for s in iv] == ["1/3", "2/3"] and iv[0]["vs_target"]["result"] == "in_target"
    assert iv[1]["vs_target"] == {"result": "below", "delta": -12} and a["intervals_in_target"] == "1/2"

    # recovery: Edge-only account -> wellness missing, load computed
    rec = m.tool("get_recovery_status", day="2026-11-13")
    assert set(rec["not_measured"]) == {"sleep", "hrv", "resting_hr", "body_battery", "training_readiness"}
    assert rec["garmin_training_status"]["status"] == "PRODUCTIVE"
    assert rec["computed_load"]["tss_last_7d"] == 50 and "computed" in rec["computed_load"]["source"]

    # strength: plan, log what was lifted, read progression
    assert m.tool("search_exercises", query="back squat")[1]["id"] == "SQUAT/BARBELL_BACK_SQUAT"
    sw = m.tool("create_strength_workout", name="Jambes", schedule_date="2026-11-24", exercises=[
        {"exercise": "Barbell Back Squat", "sets": 4, "reps": 6, "weight_kg": 90, "rest_s": 150}])
    assert sw["scheduled"]["date"] == "2026-11-24"
    assert "Close matches" in m.tool_error("create_strength_workout", name="x", exercises=[
        {"exercise": "Back Squatt", "sets": 1, "reps": 5}])
    log = m.tool("log_strength_session", session_date="2026-11-12", name="Jambes", exercises=[
        {"exercise": "SQUAT/BARBELL_BACK_SQUAT", "rest_s": 150,
         "sets": [{"reps": 5, "weight_kg": 90}, {"reps": 5, "weight_kg": 92.5}]}])
    assert log["exercises"][0]["top_set"] == "5 x 92.5 kg"
    prog = m.tool("get_strength_sessions", start="2026-11-01", end="2026-11-13", exercise="squat")
    assert prog["progression"]["Barbell Back Squat"][0]["est_1rm_kg"] == 107.9

    # body
    body = m.tool("get_body_composition", start="2026-10-01", end="2026-11-13")
    assert body["latest_weight_kg"] == 73.0 and body["ftp_w_per_kg"] == 3.77
    m.tool("add_weight", weight_kg=72.4, day="2026-11-10")
    assert fake.weighins[-1] == (72.4, "kg", "2026-11-10T08:00:00")

    # guarded deletion
    preview = m.tool("unschedule_workout", scheduled_workout_id=sid)
    assert preview["preview"]["workout"] == "SST 3x10"
    m.tool("unschedule_workout", scheduled_workout_id=sid, confirm_token=preview["confirm_token"])
    r = m.confirmed("delete_workout", workout_id=w["workout_id"])
    assert r["deleted"] == w["workout_id"]
    assert w["workout_id"] not in fake.workouts


def test_clear_error_when_garmin_not_connected(server):
    base, session = server
    m = MCP(base, _oauth(base))
    session.logged_in = False
    try:
        err = m.tool_error("list_workouts")
        assert "Garmin is not connected yet" in err and "garmin-mcp-login" in err
    finally:
        session.logged_in = True
