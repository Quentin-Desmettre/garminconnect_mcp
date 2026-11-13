"""Build Garmin Connect cycling workout JSON from a simple, LLM-friendly spec."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

CYCLING = {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}

STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "other": {"stepTypeId": 7, "stepTypeKey": "other", "displayOrder": 7},
}
REPEAT_TYPE = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}

COND_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
COND_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
COND_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
COND_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}

TGT_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
TGT_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}
TGT_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence", "displayOrder": 3}
TGT_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}


class Target(BaseModel):
    """Intensity target of a step.

    - power_watts: low/high in watts
    - power_pct_ftp: low/high in % of FTP (converted to watts with the current FTP)
    - power_zone: zone 1-7 (Garmin power zones)
    - hr_bpm: low/high in bpm
    - hr_zone: zone 1-5 (Garmin heart-rate zones)
    - cadence: low/high in rpm
    """

    kind: Literal["power_watts", "power_pct_ftp", "power_zone", "hr_bpm", "hr_zone", "cadence"]
    low: float | None = Field(None, description="Lower bound (for ranges)")
    high: float | None = Field(None, description="Upper bound (for ranges)")
    zone: int | None = Field(None, description="Zone number (for *_zone kinds)")

    @model_validator(mode="after")
    def _check(self) -> Target:
        if self.kind.endswith("_zone"):
            if self.zone is None:
                raise ValueError(f"{self.kind} requires 'zone'")
            max_zone = 7 if self.kind == "power_zone" else 5
            if not 1 <= self.zone <= max_zone:
                raise ValueError(f"{self.kind} zone must be between 1 and {max_zone}")
        else:
            if self.low is None or self.high is None:
                raise ValueError(f"{self.kind} requires 'low' and 'high'")
            if self.low > self.high:
                raise ValueError("'low' must be <= 'high'")
            if self.low <= 0:
                raise ValueError("'low' must be > 0")
        return self


class Step(BaseModel):
    """A single step. Give duration_s OR distance_m; with neither, the step ends on the lap button."""

    type: Literal["warmup", "cooldown", "interval", "recovery", "rest", "other"]
    duration_s: int | None = Field(None, gt=0, description="Step duration in seconds")
    distance_m: float | None = Field(None, gt=0, description="Step distance in meters")
    target: Target | None = Field(None, description="Main intensity target (none = free)")
    cadence: Target | None = Field(
        None, description="Optional secondary cadence target, kind must be 'cadence'"
    )
    description: str | None = Field(None, description="Note shown on the device")

    @model_validator(mode="after")
    def _check(self) -> Step:
        if self.duration_s is not None and self.distance_m is not None:
            raise ValueError("give either duration_s or distance_m, not both")
        if self.cadence is not None and self.cadence.kind != "cadence":
            raise ValueError("secondary 'cadence' target must have kind 'cadence'")
        return self


class Repeat(BaseModel):
    """Repeat a block of steps, e.g. 5 x (5 min @ 105% FTP + 3 min recovery)."""

    type: Literal["repeat"]
    iterations: int = Field(..., ge=1, le=99)
    steps: list[Step] = Field(..., min_length=1)
    skip_last_rest: bool = Field(False, description="Skip the final recovery/rest step of the last iteration")


WorkoutItem = Annotated[Step | Repeat, Field(discriminator="type")]


class WorkoutBuildError(ValueError):
    pass


def _target_fields(target: Target | None, ftp: int | None, *, secondary: bool) -> dict[str, Any]:
    prefix = "secondary" if secondary else ""

    def k(name: str) -> str:
        return f"{prefix}{name[0].upper()}{name[1:]}" if secondary else name

    if target is None:
        return {} if secondary else {"targetType": TGT_NONE}

    if target.kind in ("power_watts", "power_pct_ftp"):
        low, high = target.low, target.high
        if target.kind == "power_pct_ftp":
            if not ftp:
                raise WorkoutBuildError("power_pct_ftp needs an FTP: set it in Garmin first (set_ftp)")
            low, high = round(ftp * low / 100), round(ftp * high / 100)
        return {k("targetType"): TGT_POWER, k("targetValueOne"): float(low), k("targetValueTwo"): float(high)}
    if target.kind == "power_zone":
        return {k("targetType"): TGT_POWER, k("zoneNumber"): target.zone}
    if target.kind == "hr_bpm":
        return {k("targetType"): TGT_HR, k("targetValueOne"): target.low, k("targetValueTwo"): target.high}
    if target.kind == "hr_zone":
        return {k("targetType"): TGT_HR, k("zoneNumber"): target.zone}
    if target.kind == "cadence":
        return {k("targetType"): TGT_CADENCE, k("targetValueOne"): target.low, k("targetValueTwo"): target.high}
    raise WorkoutBuildError(f"unknown target kind {target.kind}")


def _step_duration(step: Step) -> float:
    return float(step.duration_s or 0)


class _Builder:
    def __init__(self, ftp: int | None) -> None:
        self.ftp = ftp
        self.order = 0
        self.child_id = 0

    def _next(self) -> int:
        self.order += 1
        return self.order

    def step(self, step: Step, child_step_id: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepOrder": self._next(),
            "stepType": STEP_TYPES[step.type],
        }
        if step.duration_s is not None:
            out["endCondition"], out["endConditionValue"] = COND_TIME, float(step.duration_s)
        elif step.distance_m is not None:
            out["endCondition"], out["endConditionValue"] = COND_DISTANCE, float(step.distance_m)
        else:
            out["endCondition"] = COND_LAP
        out |= _target_fields(step.target, self.ftp, secondary=False)
        out |= _target_fields(step.cadence, self.ftp, secondary=True)
        if step.description:
            out["description"] = step.description
        if child_step_id is not None:
            out["childStepId"] = child_step_id
        return out

    def repeat(self, rep: Repeat) -> dict[str, Any]:
        self.child_id += 1
        cid = self.child_id
        group: dict[str, Any] = {
            "type": "RepeatGroupDTO",
            "stepOrder": self._next(),
            "stepType": REPEAT_TYPE,
            "childStepId": cid,
            "numberOfIterations": rep.iterations,
            "endCondition": COND_ITER,
            "endConditionValue": float(rep.iterations),
            "smartRepeat": False,
        }
        if rep.skip_last_rest:
            group["skipLastRestStep"] = True
        group["workoutSteps"] = [self.step(s, child_step_id=cid) for s in rep.steps]
        return group


def estimate_duration(items: list[Step | Repeat]) -> int:
    total = 0.0
    for item in items:
        if isinstance(item, Repeat):
            total += item.iterations * sum(_step_duration(s) for s in item.steps)
        else:
            total += _step_duration(item)
    return int(total)


def build_cycling_workout(
    name: str,
    items: list[Step | Repeat],
    *,
    ftp: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Return the JSON body expected by POST /workout-service/workout."""
    if not name.strip():
        raise WorkoutBuildError("workout name must not be empty")
    if not items:
        raise WorkoutBuildError("a workout needs at least one step")
    b = _Builder(ftp)
    steps = [b.repeat(i) if isinstance(i, Repeat) else b.step(i) for i in items]
    workout: dict[str, Any] = {
        "workoutName": name.strip()[:80],
        "sportType": CYCLING,
        "estimatedDurationInSecs": estimate_duration(items),
        "workoutSegments": [{"segmentOrder": 1, "sportType": CYCLING, "workoutSteps": steps}],
    }
    if description:
        workout["description"] = description[:1024]
    return workout


def _fmt_target(step: dict[str, Any], secondary: bool = False) -> str | None:
    p = "secondary" if secondary else ""
    key = (lambda n: p + n[0].upper() + n[1:]) if secondary else (lambda n: n)
    tt = (step.get(key("targetType")) or {}).get("workoutTargetTypeKey")
    if not tt or tt == "no.target":
        return None
    zone = step.get(key("zoneNumber"))
    lo, hi = step.get(key("targetValueOne")), step.get(key("targetValueTwo"))
    unit = {"power.zone": "W", "heart.rate.zone": "bpm", "cadence": "rpm"}.get(tt, "")
    label = {"power.zone": "power", "heart.rate.zone": "HR", "cadence": "cadence"}.get(tt, tt)
    if zone:
        return f"{label} zone {zone}"
    if lo is not None and hi is not None:
        return f"{label} {lo:g}-{hi:g} {unit}"
    return label


def summarize_steps(steps: list[dict[str, Any]], indent: str = "") -> list[str]:
    """Human-readable lines for a workout's steps (as returned by Garmin)."""
    lines: list[str] = []
    for s in sorted(steps, key=lambda x: x.get("stepOrder", 0)):
        kind = (s.get("stepType") or {}).get("stepTypeKey", "?")
        if s.get("type") == "RepeatGroupDTO":
            lines.append(f"{indent}{s.get('numberOfIterations')}x:")
            lines += summarize_steps(s.get("workoutSteps") or [], indent + "  ")
            continue
        cond = (s.get("endCondition") or {}).get("conditionTypeKey")
        val = s.get("endConditionValue")
        if cond == "time" and val:
            m, sec = divmod(int(val), 60)
            dur = f"{m}:{sec:02d}"
        elif cond == "distance" and val:
            dur = f"{val / 1000:g} km"
        else:
            dur = "lap button"
        parts = [f"{indent}- {kind} {dur}"]
        for sec_flag in (False, True):
            t = _fmt_target(s, sec_flag)
            if t:
                parts.append(t)
        if s.get("description"):
            parts.append(f"({s['description']})")
        lines.append(" | ".join(parts))
    return lines
