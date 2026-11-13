"""MCP server exposing Garmin Connect: workouts, calendar, activities, recovery, strength, zones."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Literal

import anyio
import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .activities import (
    EDITABLE_TYPES,
    compare_laps,
    flatten_workout,
    summarize_activity_detail,
    summarize_activity_row,
    time_in_zones,
)
from .config import load_settings
from .confirm import ConfirmStore
from .garmin_client import GarminSession, friendly_error
from .oauth import SCOPE, OwnerOAuthProvider
from .recovery import (
    WEARABLE_NOTE,
    compute_load,
    daily_tss_from_activities,
    interpret_tsb,
    parse_body_battery,
    parse_hrv,
    parse_readiness,
    parse_sleep,
    parse_training_status,
)
from .strength import (
    PerformedExercise,
    PlannedExercise,
    build_strength_workout,
    exercise_sets_payload,
    parse_exercise_sets,
    search_exercises as _search_exercises,
)
from .workouts import Repeat, WorkoutItem, build_cycling_workout, scale_power_targets, summarize_steps
from .zones import (
    PowerZoneMode,
    describe_hr_zones,
    describe_power_zones,
    find_sport,
    updated_hr_zones,
    updated_power_zones,
)

logger = logging.getLogger("garmin_mcp")

INSTRUCTIONS = """\
Tools to manage the user's Garmin Connect account (cycling focus, plus strength):
- Read the athlete profile (FTP, power zones, heart-rate zones) before building workouts.
- Cycling workouts: steps and repeats; targets in watts, % FTP (converted to watts with the current FTP),
  power zone, bpm, HR zone or cadence. Batch tools create/schedule many at once.
- Activities: list_activities / get_activity compare planned vs actual step by step (Garmin knows which
  lap belongs to which workout step). update_activity fixes name, description or type.
- Recovery: get_recovery_status gives Garmin training status and a computed ATL/CTL/TSB.
- Strength: search_exercises, create_strength_workout (plan), log_strength_session (what was lifted),
  get_strength_sessions (history and progression).
- Guarded tools (deletions, zone/FTP changes, bulk FTP rescale) first return a preview and a confirm_token:
  show the preview to the user and only call again with the token once they agree.
Dates are YYYY-MM-DD.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

CONFIRM_FIELD = Field(None, description="Leave empty on the first call; pass the token from the preview to apply")


def _date(value: str | None, name: str, default: date | None = None) -> date:
    if value is None:
        if default is None:
            raise ToolError(f"{name} is required (YYYY-MM-DD)")
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} must be a date in YYYY-MM-DD format, got {value!r}") from None


def _workout_summary(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "workout_id": w.get("workoutId"),
        "name": w.get("workoutName"),
        "sport": (w.get("sportType") or {}).get("sportTypeKey"),
        "estimated_duration_min": round((w.get("estimatedDurationInSecs") or 0) / 60),
        "updated": w.get("updatedDate") or w.get("updateDate"),
    }


def _cycling_ftp(api: Any) -> int | None:
    try:
        zones = find_sport(api.get_power_zones(), "CYCLING")
        if zones.get("functionalThresholdPower"):
            return int(zones["functionalThresholdPower"])
    except Exception:  # noqa: BLE001 - fall back to the biometric endpoint
        logger.debug("No cycling power zones, trying biometric FTP")
    data = api.get_cycling_ftp()
    if isinstance(data, list):
        data = data[0] if data else {}
    value = (data or {}).get("functionalThresholdPower")
    return int(value) if value else None


def _uses_pct_ftp(steps: list[Any]) -> bool:
    return any(
        s.target is not None and s.target.kind == "power_pct_ftp"
        for item in steps
        for s in (item.steps if isinstance(item, Repeat) else [item])
    )


def _schedule_id(res: Any) -> Any:
    res = res or {}
    return res.get("workoutScheduleId") or res.get("id") if isinstance(res, dict) else None


def _floors(obj: dict[str, Any], n: int) -> list[float | None]:
    return [obj.get(f"zone{i}Floor") for i in range(1, n + 1)]


class WorkoutSpec(BaseModel):
    name: str = Field(..., max_length=80)
    steps: list[WorkoutItem]
    description: str | None = None
    schedule_dates: list[str] = Field(default_factory=list, description="Dates YYYY-MM-DD to schedule it on")


class ScheduleItem(BaseModel):
    workout_id: int
    date: str


def build_server(session: GarminSession, provider: OwnerOAuthProvider | None, public_url: str) -> MCPServer:
    auth = None
    if provider is not None:
        auth = AuthSettings(
            issuer_url=public_url,
            resource_server_url=f"{public_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    mcp = MCPServer(
        name="garmin-connect",
        title="Garmin Connect",
        instructions=INSTRUCTIONS,
        version="0.2.0",
        auth_server_provider=provider,
        auth=auth,
    )
    confirm = ConfirmStore()
    type_cache: dict[str, dict[str, Any]] = {}

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    if provider is not None:

        @mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
        async def consent(request: Request) -> Response:
            return await provider.consent_page(request)

    # ================================================================ profile
    @mcp.tool(annotations=READ)
    async def get_athlete_profile() -> dict[str, Any]:
        """Return the athlete's cycling FTP, power zones, heart-rate zones, weight and W/kg.

        Call this first before creating workouts so targets match the athlete's zones."""

        def run(api: Any) -> dict[str, Any]:
            out: dict[str, Any] = {"name": api.full_name, "display_name": api.display_name}
            errors: dict[str, str] = {}
            try:
                user = api.get_user_profile().get("userData", {})
                out["user"] = {k: user.get(k) for k in ("weight", "height", "birthDate", "gender", "measurementSystem",
                                                        "vo2MaxCycling", "lactateThresholdHeartRate")}
                if out["user"].get("weight"):
                    out["user"]["weight_kg"] = round(out["user"]["weight"] / 1000, 1)
            except Exception as exc:  # noqa: BLE001
                errors["user"] = str(friendly_error(exc))
            try:
                out["power_zones"] = [describe_power_zones(z) for z in api.get_power_zones()]
            except Exception as exc:  # noqa: BLE001
                errors["power_zones"] = str(friendly_error(exc))
            try:
                out["heart_rate_zones"] = [describe_hr_zones(z) for z in api.get_heart_rate_zones_v2()]
            except Exception as exc:  # noqa: BLE001
                errors["heart_rate_zones"] = str(friendly_error(exc))
            try:
                out["cycling_ftp_w"] = _cycling_ftp(api)
            except Exception as exc:  # noqa: BLE001
                errors["cycling_ftp"] = str(friendly_error(exc))
            kg = (out.get("user") or {}).get("weight_kg")
            if kg and out.get("cycling_ftp_w"):
                out["ftp_w_per_kg"] = round(out["cycling_ftp_w"] / kg, 2)
            if errors:
                out["errors"] = errors
            return out

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def set_ftp(
        ftp_watts: int = Field(..., ge=50, le=700, description="New cycling FTP in watts"),
        zones: PowerZoneMode = Field(
            "keep_percentages",
            description="How to update power zones: keep_percentages (scale current zones), "
            "coggan (reset to Coggan 7 zones), unchanged (only change FTP)",
        ),
        sport: str = Field("CYCLING", description="Power-zone sport profile, usually CYCLING"),
        confirm_token: str | None = CONFIRM_FIELD,
    ) -> dict[str, Any]:
        """Set the cycling FTP and update the power zones. Guarded: first call returns a preview to confirm.

        Workouts already created with % FTP keep their old watt targets: use update_workouts_for_ftp."""
        args = {"ftp": ftp_watts, "zones": zones, "sport": sport}

        def run(api: Any) -> dict[str, Any]:
            current = find_sport(api.get_power_zones(), sport)
            new = updated_power_zones(current, ftp=ftp_watts, mode=zones)
            if not confirm_token:
                return confirm.request("set_ftp", args, {"before": describe_power_zones(current),
                                                         "after": describe_power_zones(new)})
            confirm.check(confirm_token, "set_ftp", args)
            api.update_power_zones([new])
            after = find_sport(api.get_power_zones(), sport)
            return {"status": "applied", "before": describe_power_zones(current), "after": describe_power_zones(after)}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def set_power_zones(
        zone_floors_watts: list[int] = Field(
            ..., min_length=7, max_length=7, description="Lower bound in watts of zones 1..7, increasing"
        ),
        ftp_watts: int | None = Field(None, ge=50, le=700, description="Optionally also change FTP"),
        sport: str = Field("CYCLING", description="Power-zone sport profile"),
        confirm_token: str | None = CONFIRM_FIELD,
    ) -> dict[str, Any]:
        """Set custom power zones (7 zone floors in watts), optionally with a new FTP. Guarded (preview first)."""
        args = {"floors": zone_floors_watts, "ftp": ftp_watts, "sport": sport}

        def run(api: Any) -> dict[str, Any]:
            current = find_sport(api.get_power_zones(), sport)
            new = updated_power_zones(current, ftp=ftp_watts, floors_watts=zone_floors_watts)
            if not confirm_token:
                return confirm.request("set_power_zones", args, {"before": describe_power_zones(current),
                                                                 "after": describe_power_zones(new)})
            confirm.check(confirm_token, "set_power_zones", args)
            api.update_power_zones([new])
            after = find_sport(api.get_power_zones(), sport)
            return {"status": "applied", "before": describe_power_zones(current), "after": describe_power_zones(after)}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def set_heart_rate_zones(
        sport: str = Field(
            "DEFAULT", description="HR-zone profile: DEFAULT, CYCLING, RUNNING... (see get_athlete_profile)"
        ),
        training_method: Literal["HR_MAX", "HR_RESERVE", "LACTATE_THRESHOLD"] | None = Field(
            None, description="Zone calculation method"
        ),
        max_hr: int | None = Field(None, description="Max heart rate in bpm"),
        resting_hr: int | None = Field(None, description="Resting heart rate in bpm"),
        lthr: int | None = Field(None, description="Lactate threshold heart rate in bpm"),
        zone_floors_bpm: list[int] | None = Field(
            None, min_length=5, max_length=5,
            description="Optional explicit lower bounds (bpm) of zones 1..5. If omitted, current zones are "
            "rescaled to the new max / resting / threshold HR.",
        ),
        confirm_token: str | None = CONFIRM_FIELD,
    ) -> dict[str, Any]:
        """Update heart-rate zones and reference values (max HR, resting HR, LTHR). Guarded (preview first)."""
        args = {"sport": sport, "method": training_method, "max": max_hr, "rest": resting_hr, "lthr": lthr,
                "floors": zone_floors_bpm}

        def run(api: Any) -> dict[str, Any]:
            current = find_sport(api.get_heart_rate_zones_v2(), sport)
            new = updated_hr_zones(current, training_method=training_method, max_hr=max_hr,
                                   resting_hr=resting_hr, lthr=lthr, floors_bpm=zone_floors_bpm)
            if not confirm_token:
                return confirm.request("set_heart_rate_zones", args, {"before": describe_hr_zones(current),
                                                                      "after": describe_hr_zones(new)})
            confirm.check(confirm_token, "set_heart_rate_zones", args)
            api.update_heart_rate_zones([new])
            after = find_sport(api.get_heart_rate_zones_v2(), sport)
            return {"status": "applied", "before": describe_hr_zones(current), "after": describe_hr_zones(after)}

        return await session.acall(run)

    # ======================================================== cycling workouts
    def _create_one(api: Any, spec: WorkoutSpec, ftp_cache: dict[str, int | None]) -> dict[str, Any]:
        dates = [_date(d, "schedule date").isoformat() for d in spec.schedule_dates]
        ftp = None
        if _uses_pct_ftp(spec.steps):
            if "ftp" not in ftp_cache:
                ftp_cache["ftp"] = _cycling_ftp(api)
            ftp = ftp_cache["ftp"]
        payload = build_cycling_workout(spec.name, spec.steps, ftp=ftp, description=spec.description)
        created = api.upload_workout(payload)
        wid = created.get("workoutId")
        out: dict[str, Any] = {
            "workout_id": wid,
            "name": created.get("workoutName"),
            "ftp_used_w": ftp,
            "estimated_duration_min": round(payload["estimatedDurationInSecs"] / 60),
            "steps": summarize_steps(((created.get("workoutSegments") or [{}])[0]).get("workoutSteps") or []),
        }
        if dates and wid:
            out["scheduled"] = [{"date": d, "scheduled_workout_id": _schedule_id(api.schedule_workout(wid, d))}
                                for d in dates]
        return out

    @mcp.tool(annotations=WRITE)
    async def create_cycling_workout(
        name: str = Field(..., max_length=80, description="Workout name shown on the device"),
        steps: list[WorkoutItem] = Field(
            ...,
            description="Ordered steps. Each item is a step {type: warmup|interval|recovery|rest|cooldown|other, "
            "duration_s or distance_m, target?, cadence?, description?} or a repeat "
            "{type: 'repeat', iterations, steps: [...]}. Target: {kind: power_pct_ftp|power_watts|hr_bpm|"
            "cadence, low, high} or {kind: power_zone|hr_zone, zone}.",
        ),
        description: str | None = Field(None, description="Workout notes"),
        schedule_date: str | None = Field(None, description="Optional YYYY-MM-DD to schedule it right away"),
    ) -> dict[str, Any]:
        """Create a structured cycling workout in Garmin Connect, optionally scheduling it on the calendar.

        Example of steps for 4x8min sweet spot:
        [{"type":"warmup","duration_s":900,"target":{"kind":"power_pct_ftp","low":50,"high":65}},
         {"type":"repeat","iterations":4,"steps":[
            {"type":"interval","duration_s":480,"target":{"kind":"power_pct_ftp","low":88,"high":94}},
            {"type":"recovery","duration_s":240,"target":{"kind":"power_zone","zone":1}}]},
         {"type":"cooldown","duration_s":600}]"""
        spec = WorkoutSpec(name=name, steps=steps, description=description,
                           schedule_dates=[schedule_date] if schedule_date else [])
        return await session.acall(lambda api: _create_one(api, spec, {}))

    @mcp.tool(annotations=WRITE)
    async def create_cycling_workouts(
        workouts: list[WorkoutSpec] = Field(..., min_length=1, max_length=30,
                                            description="Workouts to create (same step format as create_cycling_workout), "
                                            "each with optional schedule_dates"),
    ) -> dict[str, Any]:
        """Create (and schedule) several cycling workouts in one call. Failures are reported per workout."""

        def run(api: Any) -> dict[str, Any]:
            cache: dict[str, int | None] = {}
            results = []
            for spec in workouts:
                try:
                    results.append({"ok": True, **_create_one(api, spec, cache)})
                except Exception as exc:  # noqa: BLE001 - reported per item
                    results.append({"ok": False, "name": spec.name, "error": str(friendly_error(exc))})
            return {"created": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results),
                    "results": results}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def update_cycling_workout(
        workout_id: int,
        name: str = Field(..., max_length=80),
        steps: list[WorkoutItem] = Field(..., description="Full new list of steps (same format as create)"),
        description: str | None = None,
    ) -> dict[str, Any]:
        """Replace the content of an existing cycling workout (keeps its id and calendar entries)."""

        def run(api: Any) -> dict[str, Any]:
            ftp = _cycling_ftp(api) if _uses_pct_ftp(steps) else None
            payload = build_cycling_workout(name, steps, ftp=ftp, description=description)
            current = api.get_workout_by_id(workout_id)
            for key in ("ownerId", "author", "workoutProvider", "workoutSourceId", "atpPlanId", "shared"):
                if key in current:
                    payload[key] = current[key]
            api.update_workout(workout_id, payload)
            updated = api.get_workout_by_id(workout_id)
            return {**_workout_summary(updated), "ftp_used_w": ftp,
                    "steps": summarize_steps(((updated.get("workoutSegments") or [{}])[0]).get("workoutSteps") or [])}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def duplicate_workout(
        workout_id: int,
        new_name: str = Field(..., max_length=80),
        schedule_dates: list[str] = Field(default_factory=list, description="Optional dates YYYY-MM-DD for the copy"),
    ) -> dict[str, Any]:
        """Copy a workout under a new name (to then tweak it with update_*), optionally scheduling the copy."""
        dates = [_date(d, "schedule date").isoformat() for d in schedule_dates]

        def run(api: Any) -> dict[str, Any]:
            res = api.copy_workout(workout_id, new_name)
            new_id = res.get("workoutId") if isinstance(res, dict) else None
            if not new_id:
                src = api.get_workout_by_id(workout_id)
                body = {k: v for k, v in src.items() if k in ("sportType", "estimatedDurationInSecs",
                                                              "workoutSegments", "description")}
                body["workoutName"] = new_name
                new_id = api.upload_workout(_strip_ids(body)).get("workoutId")
            out: dict[str, Any] = {"workout_id": new_id, "name": new_name, "copied_from": workout_id}
            if dates:
                out["scheduled"] = [{"date": d, "scheduled_workout_id": _schedule_id(api.schedule_workout(new_id, d))}
                                    for d in dates]
            return out

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def list_workouts(
        limit: int = Field(30, ge=1, le=200),
        sport: Literal["cycling", "strength_training", "all"] = "cycling",
    ) -> list[dict[str, Any]]:
        """List workouts from the Garmin Connect library, most recently updated first."""

        def run(api: Any) -> list[dict[str, Any]]:
            kwargs: dict[str, Any] = {"order_by": "UPDATE_DATE", "order_seq": "DESC"}
            if sport != "all":
                kwargs["sport_type_keys"] = [sport]
            return [_workout_summary(w) for w in api.get_workouts(0, limit, **kwargs)]

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def get_workout(workout_id: int) -> dict[str, Any]:
        """Return a workout with a readable list of its steps."""

        def run(api: Any) -> dict[str, Any]:
            w = api.get_workout_by_id(workout_id)
            steps: list[str] = []
            for seg in w.get("workoutSegments") or []:
                steps += summarize_steps(seg.get("workoutSteps") or [])
            return {**_workout_summary(w), "description": w.get("description"), "steps": steps}

        return await session.acall(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_workout(workout_id: int, confirm_token: str | None = CONFIRM_FIELD) -> dict[str, Any]:
        """Delete a workout from the library (also removes its calendar entries). Guarded (preview first)."""
        args = {"workout_id": workout_id}

        def run(api: Any) -> dict[str, Any]:
            if not confirm_token:
                w = api.get_workout_by_id(workout_id)
                return confirm.request("delete_workout", args, {"will_delete": _workout_summary(w)})
            confirm.check(confirm_token, "delete_workout", args)
            api.delete_workout(workout_id)
            return {"status": "applied", "deleted": workout_id}

        return await session.acall(run)

    # =============================================================== calendar
    @mcp.tool(annotations=WRITE)
    async def schedule_workout(
        workout_id: int,
        workout_date: str = Field(..., description="YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """Schedule an existing workout on a date in the Garmin calendar (syncs to the device)."""
        d = _date(workout_date, "workout_date").isoformat()
        return await session.acall(lambda api: {
            "workout_id": workout_id, "date": d, "scheduled_workout_id": _schedule_id(api.schedule_workout(workout_id, d))})

    @mcp.tool(annotations=WRITE)
    async def schedule_workouts(
        items: list[ScheduleItem] = Field(..., min_length=1, max_length=60),
    ) -> dict[str, Any]:
        """Schedule several existing workouts at once ([{workout_id, date}]). Failures are reported per item."""
        for it in items:
            _date(it.date, "date")

        def run(api: Any) -> dict[str, Any]:
            results = []
            for it in items:
                try:
                    results.append({"ok": True, "workout_id": it.workout_id, "date": it.date,
                                    "scheduled_workout_id": _schedule_id(api.schedule_workout(it.workout_id, it.date))})
                except Exception as exc:  # noqa: BLE001
                    results.append({"ok": False, "workout_id": it.workout_id, "date": it.date,
                                    "error": str(friendly_error(exc))})
            return {"scheduled": sum(r["ok"] for r in results), "results": results}

        return await session.acall(run)

    def _scheduled(api: Any, start_d: date, end_d: date) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        y, m = start_d.year, start_d.month
        while (y, m) <= (end_d.year, end_d.month):
            cal = api.get_scheduled_workouts(y, m) or {}
            for it in cal.get("calendarItems") or []:
                d = it.get("date") or ""
                if it.get("itemType") == "workout" and start_d.isoformat() <= d <= end_d.isoformat():
                    items.append({"scheduled_workout_id": it.get("id"), "date": d, "workout_id": it.get("workoutId"),
                                  "name": it.get("title"), "sport": it.get("sportTypeKey")})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return sorted(items, key=lambda x: x["date"])

    @mcp.tool(annotations=READ)
    async def list_scheduled_workouts(
        start: str | None = Field(None, description="YYYY-MM-DD, default today"),
        end: str | None = Field(None, description="YYYY-MM-DD, default start + 14 days"),
    ) -> list[dict[str, Any]]:
        """List workouts scheduled on the Garmin calendar between two dates."""
        start_d = _date(start, "start", date.today())
        end_d = _date(end, "end", start_d + timedelta(days=14))
        if end_d < start_d:
            raise ToolError("end must be on or after start")
        return await session.acall(lambda api: _scheduled(api, start_d, end_d))

    @mcp.tool(annotations=WRITE)
    async def reschedule_workout(
        scheduled_workout_id: int,
        new_date: str = Field(..., description="YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """Move a scheduled workout to another date."""
        d = _date(new_date, "new_date").isoformat()

        def run(api: Any) -> dict[str, Any]:
            api.reschedule_workout(scheduled_workout_id, d)
            return {"scheduled_workout_id": scheduled_workout_id, "date": d}

        return await session.acall(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def unschedule_workout(scheduled_workout_id: int, confirm_token: str | None = CONFIRM_FIELD) -> dict[str, Any]:
        """Remove a workout from the calendar (the workout stays in the library). Guarded (preview first)."""
        args = {"scheduled_workout_id": scheduled_workout_id}

        def run(api: Any) -> dict[str, Any]:
            if not confirm_token:
                entry = api.get_scheduled_workout_by_id(scheduled_workout_id) or {}
                preview = {"scheduled_workout_id": scheduled_workout_id, "date": entry.get("calendarDate"),
                           "workout": ((entry.get("workout") or {}).get("workoutName"))}
                return confirm.request("unschedule_workout", args, preview)
            confirm.check(confirm_token, "unschedule_workout", args)
            api.unschedule_workout(scheduled_workout_id)
            return {"status": "applied", "unscheduled": scheduled_workout_id}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def update_workouts_for_ftp(
        start: str = Field(..., description="First date YYYY-MM-DD of the scheduled workouts to update"),
        end: str = Field(..., description="Last date YYYY-MM-DD"),
        old_ftp_w: int = Field(..., ge=50, le=700, description="FTP the workouts were built with"),
        new_ftp_w: int = Field(..., ge=50, le=700, description="New FTP"),
        confirm_token: str | None = CONFIRM_FIELD,
    ) -> dict[str, Any]:
        """After an FTP test: rescale the watt targets of all cycling workouts scheduled in a date range.

        Targets in power zones are not touched (they follow the zones). A workout scheduled several times is
        updated once (all its dates change). Guarded: the first call returns the list of changes."""
        start_d, end_d = _date(start, "start"), _date(end, "end")
        if end_d < start_d:
            raise ToolError("end must be on or after start")
        args = {"start": start_d.isoformat(), "end": end_d.isoformat(), "old": old_ftp_w, "new": new_ftp_w}
        ratio = new_ftp_w / old_ftp_w

        def run(api: Any) -> dict[str, Any]:
            plan: dict[int, dict[str, Any]] = {}
            for it in _scheduled(api, start_d, end_d):
                wid = it["workout_id"]
                if not wid or (it.get("sport") and it["sport"] != "cycling"):
                    continue
                plan.setdefault(wid, {"workout_id": wid, "name": it["name"], "dates": []})["dates"].append(it["date"])
            updates = []
            for wid, info in plan.items():
                w = api.get_workout_by_id(wid)
                new, changes = scale_power_targets(w, ratio)
                if changes:
                    updates.append((wid, new, info | {"changes": changes}))
            preview = [u[2] for u in updates]
            if not confirm_token:
                return confirm.request("update_workouts_for_ftp", args, {
                    "ratio": round(ratio, 3), "workouts_to_update": preview,
                    "unchanged_workouts": len(plan) - len(updates)})
            confirm.check(confirm_token, "update_workouts_for_ftp", args)
            done, failed = [], []
            for wid, new, info in updates:
                try:
                    api.update_workout(wid, new)
                    done.append(info["name"])
                except Exception as exc:  # noqa: BLE001
                    failed.append({"workout_id": wid, "name": info["name"], "error": str(friendly_error(exc))})
            return {"status": "applied", "updated": done, "failed": failed}

        return await session.acall(run)

    # ============================================================= activities
    @mcp.tool(annotations=READ)
    async def list_activities(
        start: str | None = Field(None, description="YYYY-MM-DD, default 14 days ago"),
        end: str | None = Field(None, description="YYYY-MM-DD, default today"),
        activity_type: str | None = Field(None, description="Filter on type key, e.g. indoor_cycling, road_biking, "
                                          "strength_training; 'cycling' matches every bike type"),
        limit: int = Field(50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """List recorded activities (newest first) with power, HR, TSS and the linked workout if any."""
        end_d = _date(end, "end", date.today())
        start_d = _date(start, "start", end_d - timedelta(days=14))

        def run(api: Any) -> list[dict[str, Any]]:
            rows = api.get_activities_by_date(start_d.isoformat(), end_d.isoformat())
            out = []
            for a in rows:
                key = (a.get("activityType") or {}).get("typeKey", "")
                if activity_type == "cycling" and not ("cycl" in key or "biking" in key or key == "virtual_ride"):
                    continue
                if activity_type and activity_type != "cycling" and key != activity_type:
                    continue
                out.append(summarize_activity_row(a))
            return sorted(out, key=lambda r: str(r["date"]), reverse=True)[:limit]

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def get_activity(
        activity_id: int,
        include_laps: bool = Field(True, description="Include laps / planned-vs-actual per workout step"),
    ) -> dict[str, Any]:
        """Activity details. If it was a structured workout, compares each lap to its planned step
        (target vs actual power/HR/cadence, duration, rep number), plus time in power/HR zones."""

        def run(api: Any) -> dict[str, Any]:
            a = api.get_activity(activity_id)
            out = summarize_activity_detail(a)
            for label, fn in (("time_in_power_zones", api.get_activity_power_in_timezones),
                              ("time_in_hr_zones", api.get_activity_hr_in_timezones)):
                try:
                    out[label] = time_in_zones(fn(activity_id))
                except Exception:  # noqa: BLE001 - optional
                    pass
            if out["type"] == "strength_training":
                out["exercises"] = parse_exercise_sets(api.get_activity_exercise_sets(activity_id))
                return out
            if not include_laps:
                return out
            laps = (api.get_activity_laps(activity_id) or {}).get("lapDTOs") or []
            wid = out.get("workout_id")
            if wid:
                try:
                    workout = api.get_workout_by_id(wid)
                except Exception:  # noqa: BLE001 - workout deleted since
                    workout = None
                if workout:
                    out["workout"] = {"workout_id": wid, "name": workout.get("workoutName")}
                    pz = hz = None
                    max_hr = None
                    try:
                        pz = _floors(find_sport(api.get_power_zones(), "CYCLING"), 7)
                        hr = api.get_heart_rate_zones_v2()
                        hzo = next((z for z in hr if z.get("sport") == "CYCLING"), None) or find_sport(hr, "DEFAULT")
                        hz, max_hr = _floors(hzo, 5), hzo.get("maxHeartRateUsed")
                    except Exception:  # noqa: BLE001 - zones only refine the comparison
                        pass
                    out |= compare_laps(laps, flatten_workout(workout), power_floors=pz, hr_floors=hz, max_hr=max_hr)
                    return out
            out["laps"] = [
                {"lap": lap.get("lapIndex"), "duration_s": round(lap.get("duration") or 0),
                 "avg_power_w": lap.get("averagePower"), "np_w": lap.get("normalizedPower"),
                 "avg_hr": lap.get("averageHR"), "max_hr": lap.get("maxHR"), "avg_cadence": lap.get("averageBikeCadence"),
                 "intensity": lap.get("intensityType")}
                for lap in laps
            ]
            return out

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def update_activity(
        activity_id: int,
        name: str | None = Field(None, max_length=100),
        description: str | None = Field(None, max_length=2000),
        activity_type: str | None = Field(
            None, description="New type key: " + ", ".join(f"{k} ({v})" for k, v in EDITABLE_TYPES.items())),
    ) -> dict[str, Any]:
        """Fix an activity's name, description or type (e.g. a home-trainer session recorded as a road ride)."""
        if name is None and description is None and activity_type is None:
            raise ToolError("give at least one of name, description, activity_type")

        def run(api: Any) -> dict[str, Any]:
            fields: dict[str, Any] = {}
            if name is not None:
                fields["activityName"] = name
            if description is not None:
                fields["description"] = description
            if activity_type is not None:
                if not type_cache:
                    for t in api.get_activity_types() or []:
                        type_cache[t.get("typeKey")] = t
                t = type_cache.get(activity_type)
                if not t:
                    raise ToolError(f"unknown activity type {activity_type!r}; e.g. {', '.join(EDITABLE_TYPES)}")
                fields["activityTypeDTO"] = {"typeId": t["typeId"], "typeKey": t["typeKey"],
                                             "parentTypeId": t.get("parentTypeId")}
            before = summarize_activity_detail(api.get_activity(activity_id))
            api.update_activity(activity_id, fields)
            after = summarize_activity_detail(api.get_activity(activity_id))
            keys = ("name", "type", "description")
            return {"activity_id": activity_id, "before": {k: before[k] for k in keys},
                    "after": {k: after[k] for k in keys}}

        return await session.acall(run)

    # =============================================================== recovery
    @mcp.tool(annotations=READ)
    async def get_recovery_status(
        day: str | None = Field(None, description="YYYY-MM-DD, default today"),
    ) -> dict[str, Any]:
        """Fatigue / readiness picture for a day: Garmin sleep, HRV, resting HR, Body Battery and Training
        Readiness when the user has a watch; Garmin training status and load balance; and a computed
        ATL/CTL/TSB (Banister model on the last 120 days of TSS)."""
        d = _date(day, "day", date.today())

        def run(api: Any) -> dict[str, Any]:
            ds = d.isoformat()
            wellness: dict[str, Any] = {}
            missing: list[str] = []
            for label, fn in (
                ("sleep", lambda: parse_sleep(api.get_sleep_data(ds))),
                ("hrv", lambda: parse_hrv(api.get_hrv_data(ds))),
                ("resting_hr", lambda: next((r["value"] for r in api.get_rhr_daily(
                    (d - timedelta(days=1)).isoformat(), ds)[::-1]), None)),
                ("body_battery", lambda: parse_body_battery(api.get_body_battery((d - timedelta(days=1)).isoformat(), ds))),
                ("training_readiness", lambda: parse_readiness(api.get_training_readiness(ds))),
            ):
                try:
                    value = fn()
                except Exception:  # noqa: BLE001 - optional metric
                    value = None
                if value is None:
                    missing.append(label)
                else:
                    wellness[label] = value
            out: dict[str, Any] = {"date": ds, "garmin_wellness": wellness}
            if missing:
                out["not_measured"] = missing
                out["not_measured_note"] = WEARABLE_NOTE
            try:
                out["garmin_training_status"] = parse_training_status(api.get_training_status(ds))
            except Exception as exc:  # noqa: BLE001
                out["garmin_training_status_error"] = str(friendly_error(exc))
            ftp = None
            try:
                ftp = _cycling_ftp(api)
            except Exception:  # noqa: BLE001
                pass
            acts = api.get_activities_by_date((d - timedelta(days=120)).isoformat(), ds)
            load = compute_load(daily_tss_from_activities(acts, ftp=ftp), d)
            load["tsb_meaning"] = interpret_tsb(load["tsb_form"])
            load["source"] = "computed by this server from Garmin activity TSS (not a Garmin metric)"
            out["computed_load"] = load
            return out

        return await session.acall(run)

    # =============================================================== strength
    @mcp.tool(annotations=READ)
    async def search_exercises(
        query: str = Field(..., description='Words to look for, e.g. "back squat", "bench press", "pull up"'),
        limit: int = Field(15, ge=1, le=50),
    ) -> list[dict[str, str]]:
        """Search Garmin's strength exercise catalog (1500+ exercises). Use the returned name or id in
        create_strength_workout / log_strength_session."""
        return _search_exercises(query, limit)

    @mcp.tool(annotations=WRITE)
    async def create_strength_workout(
        name: str = Field(..., max_length=80),
        exercises: list[PlannedExercise] = Field(..., min_length=1, max_length=30),
        description: str | None = None,
        schedule_date: str | None = Field(None, description="Optional YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """Create a strength workout (exercise, sets, reps or time, weight, rest) and optionally schedule it.

        Note: a bike computer (Edge) cannot run strength workouts; they need a Garmin watch. They still show
        in the Garmin Connect app and calendar."""
        d = _date(schedule_date, "schedule_date").isoformat() if schedule_date else None

        def run(api: Any) -> dict[str, Any]:
            created = api.upload_workout(build_strength_workout(name, exercises, description))
            wid = created.get("workoutId")
            out = {"workout_id": wid, "name": created.get("workoutName"),
                   "estimated_duration_min": round((created.get("estimatedDurationInSecs") or 0) / 60)}
            if d and wid:
                out["scheduled"] = {"date": d, "scheduled_workout_id": _schedule_id(api.schedule_workout(wid, d))}
            return out

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def log_strength_session(
        session_date: str = Field(..., description="YYYY-MM-DD"),
        start_time: str = Field("18:00", description="Local start time HH:MM"),
        name: str = Field("Musculation", max_length=100),
        exercises: list[PerformedExercise] = Field(..., min_length=1, max_length=30,
                                                   description="What was actually done: per exercise, each set "
                                                   "with reps and weight_kg (or duration_s)"),
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Record a completed strength session in Garmin Connect (manual activity with its sets), so the
        loads actually lifted can be tracked with get_strength_sessions."""
        d = _date(session_date, "session_date")
        try:
            hh, mm = (int(x) for x in start_time.split(":"))
            start = datetime(d.year, d.month, d.day, hh, mm)
        except ValueError:
            raise ToolError("start_time must be HH:MM") from None

        def run(api: Any) -> dict[str, Any]:
            _, duration_s = exercise_sets_payload(0, start, exercises)  # validates exercises first
            tz = api.get_user_timezone() or "Europe/Paris"
            created = api.create_manual_activity(start.strftime("%Y-%m-%dT%H:%M:%S.000"), tz, "strength_training",
                                                 0, max(1, round(duration_s / 60)), name)
            aid = created.get("activityId") if isinstance(created, dict) else None
            if not aid:
                raise ToolError(f"Garmin did not return the new activity id: {str(created)[:200]}")
            payload, _ = exercise_sets_payload(int(aid), start, exercises)
            api.set_activity_exercise_sets(aid, payload)
            if notes:
                api.set_activity_description(str(aid), notes)
            return {"activity_id": aid, "name": name, "date": d.isoformat(), "duration_min": round(duration_s / 60),
                    "exercises": parse_exercise_sets(api.get_activity_exercise_sets(aid))}

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def get_strength_sessions(
        start: str | None = Field(None, description="YYYY-MM-DD, default 8 weeks ago"),
        end: str | None = Field(None, description="YYYY-MM-DD, default today"),
        exercise: str | None = Field(None, description="Only this exercise (words of its name)"),
    ) -> dict[str, Any]:
        """Strength sessions with the sets actually done (reps x kg), and per-exercise progression
        (top set, volume, estimated 1RM per session)."""
        end_d = _date(end, "end", date.today())
        start_d = _date(start, "start", end_d - timedelta(weeks=8))
        words = [w.lower() for w in (exercise or "").split()]

        def run(api: Any) -> dict[str, Any]:
            acts = [a for a in api.get_activities_by_date(start_d.isoformat(), end_d.isoformat())
                    if (a.get("activityType") or {}).get("typeKey") == "strength_training"]
            sessions, progression = [], {}
            for a in sorted(acts, key=lambda x: str(x.get("startTimeLocal"))):
                exs = parse_exercise_sets(api.get_activity_exercise_sets(a["activityId"]))
                if words:
                    exs = [e for e in exs if all(w in e["exercise"].lower() for w in words)]
                if not exs:
                    continue
                day_s = str(a.get("startTimeLocal"))[:10]
                sessions.append({"activity_id": a["activityId"], "date": day_s, "name": a.get("activityName"),
                                 "exercises": exs})
                for e in exs:
                    if e.get("top_set"):
                        progression.setdefault(e["exercise"], []).append(
                            {"date": day_s, "top_set": e["top_set"], "est_1rm_kg": e.get("est_1rm_kg"),
                             "volume_kg": e.get("volume_kg")})
            return {"sessions": sessions, "progression": progression}

        return await session.acall(run)

    # =================================================================== body
    @mcp.tool(annotations=READ)
    async def get_body_composition(
        start: str | None = Field(None, description="YYYY-MM-DD, default 90 days ago"),
        end: str | None = Field(None, description="YYYY-MM-DD, default today"),
    ) -> dict[str, Any]:
        """Weight and body composition history, with W/kg from the current FTP."""
        end_d = _date(end, "end", date.today())
        start_d = _date(start, "start", end_d - timedelta(days=90))

        def run(api: Any) -> dict[str, Any]:
            data = api.get_body_composition(start_d.isoformat(), end_d.isoformat()) or {}

            def kg(v: Any) -> float | None:
                return round(v / 1000, 1) if isinstance(v, (int, float)) else None

            rows = [{"date": r.get("calendarDate"), "weight_kg": kg(r.get("weight")), "body_fat_pct": r.get("bodyFat"),
                     "muscle_mass_kg": kg(r.get("muscleMass")), "bmi": r.get("bmi"), "source": r.get("sourceType")}
                    for r in data.get("dateWeightList") or []]
            out: dict[str, Any] = {"measurements": sorted(rows, key=lambda r: str(r["date"]))}
            latest = next((r["weight_kg"] for r in reversed(out["measurements"]) if r["weight_kg"]), None)
            if latest is None:
                latest = kg((api.get_latest_weight(end_d.isoformat()) or {}).get("weight"))
            out["latest_weight_kg"] = latest
            ftp = _cycling_ftp(api)
            if ftp and latest:
                out["ftp_w"], out["ftp_w_per_kg"] = ftp, round(ftp / latest, 2)
            return out

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def add_weight(
        weight_kg: float = Field(..., ge=30, le=250),
        day: str | None = Field(None, description="YYYY-MM-DD, default today"),
    ) -> dict[str, Any]:
        """Record a weigh-in in Garmin Connect."""
        d = _date(day, "day", date.today())

        def run(api: Any) -> dict[str, Any]:
            ts = "" if d == date.today() else f"{d.isoformat()}T08:00:00"
            api.add_weigh_in(weight_kg, "kg", ts)
            return {"recorded": {"date": d.isoformat(), "weight_kg": weight_kg}}

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def garmin_status() -> dict[str, Any]:
        """Check that the server is connected to Garmin Connect (and why not, if it is not)."""
        return await anyio.to_thread.run_sync(session.status)

    return mcp


def _strip_ids(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_ids(v) for k, v in obj.items() if k not in ("workoutId", "stepId", "ownerId")}
    if isinstance(obj, list):
        return [_strip_ids(v) for v in obj]
    return obj


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    session = GarminSession(settings.garmin_tokens_path)
    provider = OwnerOAuthProvider(settings)
    mcp = build_server(session, provider, settings.public_url)
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host=settings.host)
    logger.info("Garmin MCP listening on %s:%s, public URL %s/mcp", settings.host, settings.port, settings.public_url)
    status = session.status()
    if not status["connected"]:
        logger.warning("Garmin not connected yet: %s", status.get("error"))
    session.start_keepalive()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
