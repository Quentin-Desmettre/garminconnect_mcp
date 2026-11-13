"""Strength workouts: exercise catalog, workout builder, logged sets, progression (pure functions)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from garminconnect import exercises as catalog
from garminconnect.workout import (
    StrengthWorkout,
    WorkoutSegment,
    create_strength_set,
    create_strength_timed_set,
)
from pydantic import BaseModel, Field, model_validator

STRENGTH_SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
SECONDS_PER_REP = 3.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def search_exercises(query: str, limit: int = 15) -> list[dict[str, str]]:
    """Find catalog exercises; every word of the query must appear (hyphens/spaces ignored)."""
    words = [_norm(w) for w in query.split() if _norm(w)]
    if not words:
        return []
    hits = []
    for e in catalog.EXERCISES:
        hay = _norm(e["name"]) + " " + _norm(e["category"]) + " " + _norm(e["exercise"])
        if all(w in hay for w in words):
            hits.append(e)
    hits.sort(key=lambda e: (len(e["name"]), e["name"]))
    return [{"name": e["name"], "id": f"{e['category']}/{e['exercise']}"} for e in hits[:limit]]


def resolve_exercise(ref: str) -> dict[str, str]:
    """Accept a catalog display name ("Barbell Back Squat") or an id ("SQUAT/BARBELL_BACK_SQUAT")."""
    ref = ref.strip()
    if "/" in ref:
        cat, ex = (p.strip().upper() for p in ref.split("/", 1))
        for e in catalog.EXERCISES:
            if e["category"] == cat and e["exercise"] == ex:
                return e
        if cat in catalog.CATEGORIES and not ex:
            return {"name": cat.title(), "category": cat, "exercise": ""}
        raise ValueError(f"unknown exercise id {ref!r}: use search_exercises")
    exact = {_norm(e["name"]): e for e in catalog.EXERCISES}.get(_norm(ref))
    if exact:
        return exact
    suggestions = [h["name"] for h in search_exercises(ref, 5)]
    raise ValueError(f"unknown exercise {ref!r}. Close matches: {suggestions or 'none'} (see search_exercises)")


class PlannedExercise(BaseModel):
    exercise: str = Field(..., description='Catalog name ("Barbell Back Squat") or id ("SQUAT/BARBELL_BACK_SQUAT")')
    sets: int = Field(..., ge=1, le=20)
    reps: int | None = Field(None, ge=1, le=100, description="Reps per set (or use duration_s)")
    duration_s: int | None = Field(None, ge=5, le=3600, description="Time per set, e.g. a plank")
    weight_kg: float | None = Field(None, ge=0, le=500)
    rest_s: int = Field(90, ge=0, le=900)
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PlannedExercise:
        if (self.reps is None) == (self.duration_s is None):
            raise ValueError("give either reps or duration_s")
        return self


def build_strength_workout(name: str, items: list[PlannedExercise], description: str | None = None) -> dict[str, Any]:
    if not items:
        raise ValueError("a strength workout needs at least one exercise")
    steps = []
    order = 1
    total = 0.0
    for it in items:
        ex = resolve_exercise(it.exercise)
        kwargs = dict(exercise_name=ex["exercise"], weight_kg=it.weight_kg, description=it.notes)
        if it.reps is not None:
            group = create_strength_set(ex["category"], order, it.sets, it.reps, it.rest_s, **kwargs)
            work = it.reps * SECONDS_PER_REP
        else:
            group = create_strength_timed_set(ex["category"], order, it.sets, it.duration_s, it.rest_s, **kwargs)
            work = float(it.duration_s or 0)
        steps.append(group)
        order += 3
        total += it.sets * (work + it.rest_s)
    w = StrengthWorkout(
        workoutName=name.strip()[:80],
        estimatedDurationInSecs=int(total),
        workoutSegments=[WorkoutSegment(segmentOrder=1, sportType=STRENGTH_SPORT, workoutSteps=steps)],
        description=description,
    )
    return w.to_dict()


class PerformedSet(BaseModel):
    reps: int | None = Field(None, ge=0, le=200)
    weight_kg: float | None = Field(None, ge=0, le=500)
    duration_s: int | None = Field(None, ge=1, le=3600)


class PerformedExercise(BaseModel):
    exercise: str = Field(..., description="Catalog name or id, as for workouts")
    sets: list[PerformedSet] = Field(..., min_length=1)
    rest_s: int = Field(90, ge=0, le=900, description="Rest after each set")


def exercise_sets_payload(activity_id: int, start: datetime, items: list[PerformedExercise]) -> tuple[dict[str, Any], int]:
    """Body for PUT /activity/{id}/exerciseSets, plus the total duration in seconds.

    Garmin stores ``weight`` in grams.
    """
    sets: list[dict[str, Any]] = []
    t = start
    for it in items:
        ex = resolve_exercise(it.exercise)
        for s in it.sets:
            dur = float(s.duration_s or (s.reps or 0) * SECONDS_PER_REP or 30)
            sets.append({
                "exercises": [{"category": ex["category"], "name": ex["exercise"] or None, "probability": 100.0}],
                "duration": dur,
                "repetitionCount": s.reps,
                "weight": None if s.weight_kg is None else s.weight_kg * 1000.0,
                "setType": "ACTIVE",
                "startTime": t.strftime("%Y-%m-%dT%H:%M:%S.0"),
                "wktStepIndex": None,
                "messageIndex": len(sets),
            })
            t += timedelta(seconds=dur)
            if it.rest_s:
                sets.append({
                    "exercises": [], "duration": float(it.rest_s), "repetitionCount": None, "weight": None,
                    "setType": "REST", "startTime": t.strftime("%Y-%m-%dT%H:%M:%S.0"), "wktStepIndex": None,
                    "messageIndex": len(sets),
                })
                t += timedelta(seconds=it.rest_s)
    return {"activityId": activity_id, "exerciseSets": sets}, int((t - start).total_seconds())


def _display(category: str | None, name: str | None) -> str:
    for e in catalog.EXERCISES:
        if e["category"] == category and (e["exercise"] or None) == (name or None):
            return e["name"]
    return f"{category}/{name}" if name else str(category)


def epley_1rm(weight: float, reps: int) -> float:
    return weight if reps <= 1 else weight * (1 + reps / 30)


def parse_exercise_sets(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Group the active sets of a strength activity by exercise."""
    by_ex: dict[str, dict[str, Any]] = {}
    for s in (data or {}).get("exerciseSets") or []:
        if s.get("setType") != "ACTIVE":
            continue
        exs = s.get("exercises") or [{}]
        best = max(exs, key=lambda e: e.get("probability") or 0)
        label = _display(best.get("category"), best.get("name"))
        kg = s.get("weight")
        kg = round(kg / 1000, 2) if isinstance(kg, (int, float)) and kg > 0 else None
        reps = s.get("repetitionCount")
        entry = by_ex.setdefault(label, {"exercise": label, "sets": []})
        row: dict[str, Any] = {"reps": reps, "weight_kg": kg}
        if s.get("duration") and not reps:
            row["duration_s"] = round(s["duration"])
        entry["sets"].append(row)
    for entry in by_ex.values():
        loaded = [(s["weight_kg"], s["reps"]) for s in entry["sets"] if s["weight_kg"] and s["reps"]]
        if loaded:
            top = max(loaded)
            entry["top_set"] = f"{top[1]} x {top[0]:g} kg"
            entry["volume_kg"] = round(sum(w * r for w, r in loaded))
            entry["est_1rm_kg"] = round(max(epley_1rm(w, r) for w, r in loaded), 1)
    return list(by_ex.values())
