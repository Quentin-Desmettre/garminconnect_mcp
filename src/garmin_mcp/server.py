"""MCP server exposing Garmin Connect cycling workouts, calendar, FTP and zones."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

import anyio
import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import load_settings
from .garmin_client import GarminSession
from .oauth import SCOPE, OwnerOAuthProvider
from .workouts import WorkoutItem, build_cycling_workout, summarize_steps
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
Tools to manage the user's Garmin Connect account (cycling focus):
- Read the athlete profile (FTP, power zones, heart-rate zones) before building workouts.
- Build structured cycling workouts with steps and repeats; targets can be in watts,
  % FTP (converted to watts with the current FTP), power zone, bpm, HR zone or cadence.
- Schedule workouts on the Garmin calendar; they sync to the user's device.
- Update FTP and power / heart-rate zones.
Dates are YYYY-MM-DD. Confirm with the user before deleting workouts or changing zones.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


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
        version="0.1.0",
        auth_server_provider=provider,
        auth=auth,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    if provider is not None:

        @mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
        async def consent(request: Request) -> Response:
            return await provider.consent_page(request)

    # ---------------------------------------------------------------- profile
    @mcp.tool(annotations=READ)
    async def get_athlete_profile() -> dict[str, Any]:
        """Return the athlete's cycling FTP, power zones, heart-rate zones and basic user data.

        Call this first before creating workouts so targets match the athlete's zones."""

        def run(api: Any) -> dict[str, Any]:
            out: dict[str, Any] = {"name": api.full_name, "display_name": api.display_name}
            try:
                user = api.get_user_profile().get("userData", {})
                out["user"] = {
                    k: user.get(k)
                    for k in ("weight", "height", "birthDate", "gender", "measurementSystem",
                              "vo2MaxCycling", "lactateThresholdHeartRate")
                }
                if out["user"].get("weight"):
                    out["user"]["weight_kg"] = round(out["user"]["weight"] / 1000, 1)
            except Exception as exc:  # noqa: BLE001
                out["user_error"] = str(exc)
            try:
                out["power_zones"] = [describe_power_zones(z) for z in api.get_power_zones()]
            except Exception as exc:  # noqa: BLE001
                out["power_zones_error"] = str(exc)
            try:
                out["heart_rate_zones"] = [describe_hr_zones(z) for z in api.get_heart_rate_zones_v2()]
            except Exception as exc:  # noqa: BLE001
                out["heart_rate_zones_error"] = str(exc)
            try:
                out["cycling_ftp_w"] = _cycling_ftp(api)
            except Exception as exc:  # noqa: BLE001
                out["cycling_ftp_error"] = str(exc)
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
    ) -> dict[str, Any]:
        """Set the cycling FTP in Garmin Connect and update the power zones accordingly."""

        def run(api: Any) -> dict[str, Any]:
            all_zones = api.get_power_zones()
            current = find_sport(all_zones, sport)
            new = updated_power_zones(current, ftp=ftp_watts, mode=zones)
            api.update_power_zones([new])
            after = find_sport(api.get_power_zones(), sport)
            return {"before": describe_power_zones(current), "after": describe_power_zones(after)}

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def set_power_zones(
        zone_floors_watts: list[int] = Field(
            ..., min_length=7, max_length=7, description="Lower bound in watts of zones 1..7, increasing"
        ),
        ftp_watts: int | None = Field(None, ge=50, le=700, description="Optionally also change FTP"),
        sport: str = Field("CYCLING", description="Power-zone sport profile"),
    ) -> dict[str, Any]:
        """Set custom power zones (7 zone floors in watts), optionally with a new FTP."""

        def run(api: Any) -> dict[str, Any]:
            current = find_sport(api.get_power_zones(), sport)
            new = updated_power_zones(current, ftp=ftp_watts, floors_watts=zone_floors_watts)
            api.update_power_zones([new])
            after = find_sport(api.get_power_zones(), sport)
            return {"before": describe_power_zones(current), "after": describe_power_zones(after)}

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
    ) -> dict[str, Any]:
        """Update heart-rate zones and reference values (max HR, resting HR, LTHR) for a sport profile."""

        def run(api: Any) -> dict[str, Any]:
            current = find_sport(api.get_heart_rate_zones_v2(), sport)
            new = updated_hr_zones(
                current, training_method=training_method, max_hr=max_hr,
                resting_hr=resting_hr, lthr=lthr, floors_bpm=zone_floors_bpm,
            )
            api.update_heart_rate_zones([new])
            after = find_sport(api.get_heart_rate_zones_v2(), sport)
            return {"before": describe_hr_zones(current), "after": describe_hr_zones(after)}

        return await session.acall(run)

    # --------------------------------------------------------------- workouts
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
        if schedule_date:
            date.fromisoformat(schedule_date)

        def run(api: Any) -> dict[str, Any]:
            uses_pct = any(
                (s.target and s.target.kind == "power_pct_ftp")
                for item in steps
                for s in (item.steps if item.type == "repeat" else [item])  # type: ignore[union-attr]
            )
            ftp = _cycling_ftp(api) if uses_pct else None
            payload = build_cycling_workout(name, steps, ftp=ftp, description=description)
            created = api.upload_workout(payload)
            workout_id = created.get("workoutId")
            out: dict[str, Any] = {
                "workout_id": workout_id,
                "name": created.get("workoutName"),
                "ftp_used_w": ftp,
                "estimated_duration_min": round(payload["estimatedDurationInSecs"] / 60),
                "steps": summarize_steps(
                    ((created.get("workoutSegments") or [{}])[0]).get("workoutSteps") or []
                ),
            }
            if schedule_date and workout_id:
                sched = api.schedule_workout(workout_id, schedule_date)
                out["scheduled"] = {
                    "date": schedule_date,
                    "scheduled_workout_id": (sched or {}).get("workoutScheduleId") or (sched or {}).get("id"),
                }
            return out

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
            uses_pct = any(
                (s.target and s.target.kind == "power_pct_ftp")
                for item in steps
                for s in (item.steps if item.type == "repeat" else [item])  # type: ignore[union-attr]
            )
            ftp = _cycling_ftp(api) if uses_pct else None
            payload = build_cycling_workout(name, steps, ftp=ftp, description=description)
            current = api.get_workout_by_id(workout_id)
            for key in ("ownerId", "author", "workoutProvider", "workoutSourceId", "atpPlanId", "shared"):
                if key in current:
                    payload[key] = current[key]
            api.update_workout(workout_id, payload)
            updated = api.get_workout_by_id(workout_id)
            return {
                **_workout_summary(updated),
                "ftp_used_w": ftp,
                "steps": summarize_steps(((updated.get("workoutSegments") or [{}])[0]).get("workoutSteps") or []),
            }

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def list_workouts(
        limit: int = Field(30, ge=1, le=200),
        cycling_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List workouts from the Garmin Connect library, most recently updated first."""

        def run(api: Any) -> list[dict[str, Any]]:
            kwargs: dict[str, Any] = {"order_by": "UPDATE_DATE", "order_seq": "DESC"}
            if cycling_only:
                kwargs["sport_type_keys"] = ["cycling"]
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
    async def delete_workout(workout_id: int) -> dict[str, Any]:
        """Delete a workout from the library (also removes its calendar entries). Confirm with the user first."""

        def run(api: Any) -> dict[str, Any]:
            api.delete_workout(workout_id)
            return {"deleted": workout_id}

        return await session.acall(run)

    # --------------------------------------------------------------- calendar
    @mcp.tool(annotations=WRITE)
    async def schedule_workout(
        workout_id: int,
        workout_date: str = Field(..., description="YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """Schedule an existing workout on a date in the Garmin calendar (syncs to the device)."""
        date.fromisoformat(workout_date)
        date_str = workout_date

        def run(api: Any) -> dict[str, Any]:
            res = api.schedule_workout(workout_id, date_str) or {}
            return {
                "workout_id": workout_id,
                "date": date_str,
                "scheduled_workout_id": res.get("workoutScheduleId") or res.get("id"),
            }

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def list_scheduled_workouts(
        start: str | None = Field(None, description="YYYY-MM-DD, default today"),
        end: str | None = Field(None, description="YYYY-MM-DD, default start + 14 days"),
    ) -> list[dict[str, Any]]:
        """List workouts scheduled on the Garmin calendar between two dates."""
        start_d = date.fromisoformat(start) if start else date.today()
        end_d = date.fromisoformat(end) if end else start_d + timedelta(days=14)
        if end_d < start_d:
            raise ValueError("end must be >= start")

        def run(api: Any) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            y, m = start_d.year, start_d.month
            while (y, m) <= (end_d.year, end_d.month):
                cal = api.get_scheduled_workouts(y, m) or {}
                for it in cal.get("calendarItems") or []:
                    d = it.get("date") or ""
                    if it.get("itemType") == "workout" and start_d.isoformat() <= d <= end_d.isoformat():
                        items.append({
                            "scheduled_workout_id": it.get("id"),
                            "date": d,
                            "workout_id": it.get("workoutId"),
                            "name": it.get("title"),
                            "sport": it.get("sportTypeKey"),
                        })
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return sorted(items, key=lambda x: x["date"])

        return await session.acall(run)

    @mcp.tool(annotations=WRITE)
    async def reschedule_workout(
        scheduled_workout_id: int,
        new_date: str = Field(..., description="YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """Move a scheduled workout to another date."""
        date.fromisoformat(new_date)

        def run(api: Any) -> dict[str, Any]:
            api.reschedule_workout(scheduled_workout_id, new_date)
            return {"scheduled_workout_id": scheduled_workout_id, "date": new_date}

        return await session.acall(run)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def unschedule_workout(scheduled_workout_id: int) -> dict[str, Any]:
        """Remove a workout from the calendar (the workout stays in the library)."""

        def run(api: Any) -> dict[str, Any]:
            api.unschedule_workout(scheduled_workout_id)
            return {"unscheduled": scheduled_workout_id}

        return await session.acall(run)

    @mcp.tool(annotations=READ)
    async def garmin_status() -> dict[str, Any]:
        """Check that the server is connected to Garmin Connect."""
        return await anyio.to_thread.run_sync(session.status)

    return mcp


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
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
