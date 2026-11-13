"""Activity summaries and planned-vs-actual comparison, from Garmin JSON (pure functions)."""

from __future__ import annotations

from typing import Any

# Activity types the LLM may set on an activity (typeKey -> label)
EDITABLE_TYPES = {
    "indoor_cycling": "home trainer / indoor",
    "virtual_ride": "virtual ride (Zwift...)",
    "road_biking": "road ride outdoors",
    "gravel_cycling": "gravel",
    "mountain_biking": "MTB",
    "cycling": "generic cycling",
    "strength_training": "strength",
}


def _r(v: Any, nd: int = 0) -> Any:
    if isinstance(v, (int, float)):
        return round(v, nd) if nd else int(round(v))
    return v


def summarize_activity_row(a: dict[str, Any]) -> dict[str, Any]:
    """Compact summary of an entry from the activity list."""
    return {
        "activity_id": a.get("activityId"),
        "date": a.get("startTimeLocal"),
        "name": a.get("activityName"),
        "type": (a.get("activityType") or {}).get("typeKey"),
        "duration_min": _r((a.get("duration") or 0) / 60),
        "distance_km": _r((a.get("distance") or 0) / 1000, 1),
        "avg_power_w": _r(a.get("avgPower")),
        "np_w": _r(a.get("normPower")),
        "avg_hr": _r(a.get("averageHR")),
        "max_hr": _r(a.get("maxHR")),
        "avg_cadence": _r(a.get("averageBikingCadenceInRevPerMinute")),
        "tss": _r(a.get("trainingStressScore"), 1),
        "intensity_factor": _r(a.get("intensityFactor"), 2),
        "training_load": _r(a.get("activityTrainingLoad"), 1),
        "workout_id": a.get("workoutId"),
    }


def summarize_activity_detail(a: dict[str, Any]) -> dict[str, Any]:
    s = a.get("summaryDTO") or {}
    return {
        "activity_id": a.get("activityId"),
        "name": a.get("activityName"),
        "description": a.get("description"),
        "type": (a.get("activityTypeDTO") or {}).get("typeKey"),
        "start_local": s.get("startTimeLocal"),
        "duration_min": _r((s.get("duration") or 0) / 60, 1),
        "moving_min": _r((s.get("movingDuration") or 0) / 60, 1),
        "distance_km": _r((s.get("distance") or 0) / 1000, 1),
        "avg_power_w": _r(s.get("averagePower")),
        "np_w": _r(s.get("normalizedPower")),
        "max_power_w": _r(s.get("maxPower")),
        "best_20min_w": _r(s.get("maxPowerTwentyMinutes")),
        "avg_hr": _r(s.get("averageHR")),
        "max_hr": _r(s.get("maxHR")),
        "avg_cadence": _r(s.get("averageBikeCadence")),
        "ftp_at_time_w": _r(s.get("functionalThresholdPower")),
        "tss": _r(s.get("trainingStressScore"), 1),
        "intensity_factor": _r(s.get("intensityFactor"), 2),
        "training_load": _r(s.get("activityTrainingLoad"), 1),
        "aerobic_te": _r(s.get("trainingEffect"), 1),
        "anaerobic_te": _r(s.get("anaerobicTrainingEffect"), 1),
        "te_label": s.get("trainingEffectLabel"),
        "calories": _r(s.get("calories")),
        "workout_id": (a.get("metadataDTO") or {}).get("associatedWorkoutId"),
        "device": ((a.get("metadataDTO") or {}).get("deviceMetaDataDTO") or {}).get("deviceTypePk"),
    }


def flatten_workout(workout: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the workout steps in FIT order, which is what ``lap.wktStepIndex`` refers to.

    Executable steps are numbered in order; a repeat group takes the index
    *after* its children (verified against real Edge 830 recordings).
    """
    flat: list[dict[str, Any]] = []

    def walk(steps: list[dict[str, Any]], repeat: int | None) -> None:
        for s in sorted(steps, key=lambda x: x.get("stepOrder", 0)):
            if s.get("type") == "RepeatGroupDTO":
                walk(s.get("workoutSteps") or [], s.get("numberOfIterations"))
                flat.append({"kind": "repeat", "iterations": s.get("numberOfIterations")})
            else:
                flat.append(_planned_step(s, repeat))

    for seg in workout.get("workoutSegments") or []:
        walk(seg.get("workoutSteps") or [], None)
    return flat


def _planned_step(s: dict[str, Any], repeat: int | None) -> dict[str, Any]:
    cond = (s.get("endCondition") or {}).get("conditionTypeKey")
    tt = (s.get("targetType") or {}).get("workoutTargetTypeKey")
    target: dict[str, Any] | None = None
    if tt in ("power.zone", "heart.rate.zone", "cadence"):
        kind = {"power.zone": "power", "heart.rate.zone": "hr", "cadence": "cadence"}[tt]
        target = {"kind": kind}
        if s.get("zoneNumber"):
            target["zone"] = s["zoneNumber"]
        else:
            target["low"], target["high"] = s.get("targetValueOne"), s.get("targetValueTwo")
    out: dict[str, Any] = {
        "kind": (s.get("stepType") or {}).get("stepTypeKey"),
        "planned_duration_s": s.get("endConditionValue") if cond == "time" else None,
        "planned_distance_m": s.get("endConditionValue") if cond == "distance" else None,
        "target": target,
    }
    if repeat:
        out["repeat_of"] = repeat
    if s.get("description"):
        out["description"] = s["description"]
    return out


def _zone_range(zone: int, floors: list[float | None], top: float | None) -> tuple[float | None, float | None]:
    if not floors or zone < 1 or zone > len(floors):
        return None, None
    lo = floors[zone - 1]
    hi = floors[zone] - 1 if zone < len(floors) and floors[zone] else top
    return lo, hi


def _verdict(value: float | None, lo: float | None, hi: float | None) -> dict[str, Any] | None:
    if value is None or lo is None or hi is None:
        return None
    if value < lo:
        return {"result": "below", "delta": round(value - lo)}
    if value > hi:
        return {"result": "above", "delta": round(value - hi)}
    return {"result": "in_target", "delta": 0}


def compare_laps(
    laps: list[dict[str, Any]],
    flat: list[dict[str, Any]],
    *,
    power_floors: list[float | None] | None = None,
    hr_floors: list[float | None] | None = None,
    max_hr: float | None = None,
) -> dict[str, Any]:
    """Planned vs actual, lap by lap. Laps without a workout step are listed apart."""
    rep_count: dict[int, int] = {}
    steps: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for lap in sorted(laps, key=lambda x: x.get("lapIndex", 0)):
        actual = {
            "lap": lap.get("lapIndex"),
            "duration_s": _r(lap.get("duration")),
            "avg_power_w": _r(lap.get("averagePower")),
            "np_w": _r(lap.get("normalizedPower")),
            "max_power_w": _r(lap.get("maxPower")),
            "avg_hr": _r(lap.get("averageHR")),
            "max_hr": _r(lap.get("maxHR")),
            "avg_cadence": _r(lap.get("averageBikeCadence")),
        }
        idx = lap.get("wktStepIndex")
        if idx is None or not isinstance(idx, int) or idx >= len(flat) or flat[idx]["kind"] == "repeat":
            unmatched.append(actual | {"intensity": lap.get("intensityType")})
            continue
        plan = flat[idx]
        rep_count[idx] = rep_count.get(idx, 0) + 1
        row: dict[str, Any] = {"step_index": idx, "step": plan["kind"]}
        if plan.get("repeat_of"):
            row["rep"] = f"{rep_count[idx]}/{plan['repeat_of']}"
        row["planned"] = {k: v for k, v in plan.items() if k in ("planned_duration_s", "planned_distance_m", "target")
                          and v is not None}
        row["actual"] = actual
        tgt = plan.get("target")
        if tgt:
            lo, hi = tgt.get("low"), tgt.get("high")
            if "zone" in tgt:
                floors = power_floors if tgt["kind"] == "power" else hr_floors if tgt["kind"] == "hr" else None
                lo, hi = _zone_range(tgt["zone"], floors or [], None if tgt["kind"] == "power" else max_hr)
                if lo is not None:
                    row["planned"]["target_resolved"] = {"low": lo, "high": hi}
            value = {"power": actual["avg_power_w"], "hr": actual["avg_hr"], "cadence": actual["avg_cadence"]}[tgt["kind"]]
            v = _verdict(value, lo, hi)
            if v:
                row["vs_target"] = v
        pd = plan.get("planned_duration_s")
        if pd and actual["duration_s"] is not None and actual["duration_s"] < 0.9 * pd:
            row["note"] = f"stopped early: {actual['duration_s']} s of {int(pd)} s planned"
        steps.append(row)
    out: dict[str, Any] = {"steps": steps}
    if unmatched:
        out["laps_without_step"] = unmatched
        out["laps_without_step_note"] = (
            "These laps are not tied to a workout step (workout ended or stopped early, extra laps)."
        )
    hits = [s["vs_target"]["result"] for s in steps if "vs_target" in s and s["step"] == "interval"]
    if hits:
        out["intervals_in_target"] = f"{hits.count('in_target')}/{len(hits)}"
    return out


def time_in_zones(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"zone": r.get("zoneNumber"), "minutes": round((r.get("secsInZone") or 0) / 60, 1), "from": r.get("zoneLowBoundary")}
        for r in rows or []
    ]
