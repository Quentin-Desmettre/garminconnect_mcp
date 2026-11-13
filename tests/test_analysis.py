from datetime import date, datetime, timedelta

import pytest

from garmin_mcp.activities import compare_laps, flatten_workout
from garmin_mcp.recovery import compute_load, daily_tss_from_activities, parse_training_status
from garmin_mcp.strength import (
    PerformedExercise,
    PlannedExercise,
    build_strength_workout,
    exercise_sets_payload,
    parse_exercise_sets,
    resolve_exercise,
    search_exercises,
)
from garmin_mcp.workouts import scale_power_targets


def _step(order, kind, secs, lo=None, hi=None, child=None):
    s = {"type": "ExecutableStepDTO", "stepOrder": order, "stepType": {"stepTypeKey": kind},
         "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": secs, "childStepId": child,
         "targetType": {"workoutTargetTypeKey": "power.zone" if lo else "no.target"}}
    if lo:
        s |= {"targetValueOne": lo, "targetValueTwo": hi}
    return s


def _repeat(order, n, child, steps):
    return {"type": "RepeatGroupDTO", "stepOrder": order, "numberOfIterations": n, "childStepId": child,
            "stepType": {"stepTypeKey": "repeat"}, "workoutSteps": steps}


# Shape of the real "Seuil 3x15 min" workout (Garmin Connect, Edge 830)
SEUIL = {"workoutName": "Seuil 3x15 min", "workoutSegments": [{"workoutSteps": [
    _step(1, "warmup", 480),
    _repeat(2, 3, 1, [_step(3, "interval", 60, 185, 195, 1), _step(4, "recovery", 60, 50, 95, 1)]),
    _step(5, "warmup", 60),
    _repeat(6, 3, 2, [_step(7, "interval", 900, 163, 172, 2), _step(8, "recovery", 300, 50, 95, 2)]),
    _step(9, "cooldown", 600),
]}]}


def _lap(i, wkt, dur, p, hr=140, intensity="ACTIVE"):
    return {"lapIndex": i, "wktStepIndex": wkt, "duration": dur, "averagePower": p, "normalizedPower": p,
            "averageHR": hr, "maxHR": hr + 5, "averageBikeCadence": 90, "intensityType": intensity}


LAPS = [
    _lap(1, 0, 480, 86, intensity="WARMUP"),
    _lap(2, 1, 60, 191), _lap(3, 2, 60, 93), _lap(4, 1, 60, 187), _lap(5, 2, 60, 86), _lap(6, 1, 60, 192),
    _lap(7, 2, 60, 85), _lap(8, 4, 60, 82),
    _lap(9, 5, 900, 169), _lap(10, 6, 300, 90), _lap(11, 5, 900, 175), _lap(12, 6, 300, 81),
    _lap(13, 5, 132, 172), _lap(14, None, 777.7, 174),
]


def test_flatten_matches_fit_indexes():
    flat = flatten_workout(SEUIL)
    assert [f["kind"] for f in flat] == ["warmup", "interval", "recovery", "repeat", "warmup", "interval",
                                         "recovery", "repeat", "cooldown"]
    assert flat[5]["target"] == {"kind": "power", "low": 163, "high": 172} and flat[5]["repeat_of"] == 3


def test_compare_laps_planned_vs_actual():
    res = compare_laps(LAPS, flatten_workout(SEUIL))
    intervals = [s for s in res["steps"] if s["step"] == "interval"]
    assert [s["rep"] for s in intervals] == ["1/3", "2/3", "3/3", "1/3", "2/3", "3/3"]
    assert intervals[3]["vs_target"] == {"result": "in_target", "delta": 0}
    assert intervals[4]["vs_target"] == {"result": "above", "delta": 3}
    assert "stopped early" in intervals[5]["note"]
    assert res["laps_without_step"][0]["lap"] == 14
    assert res["intervals_in_target"] == "5/6"


def test_compare_zone_targets_resolved():
    w = {"workoutSegments": [{"workoutSteps": [
        {"type": "ExecutableStepDTO", "stepOrder": 1, "stepType": {"stepTypeKey": "interval"},
         "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 600,
         "targetType": {"workoutTargetTypeKey": "power.zone"}, "zoneNumber": 2}]}]}
    res = compare_laps([_lap(1, 0, 600, 120)], flatten_workout(w), power_floors=[0, 95, 130, 155, 172, 190, 220])
    step = res["steps"][0]
    assert step["planned"]["target_resolved"] == {"low": 95, "high": 129}
    assert step["vs_target"]["result"] == "in_target"


def test_scale_power_targets_only_watts():
    new, changes = scale_power_targets(SEUIL, 180 / 172)
    steps = new["workoutSegments"][0]["workoutSteps"]
    assert steps[3]["workoutSteps"][0]["targetValueOne"] == 171 and steps[3]["workoutSteps"][0]["targetValueTwo"] == 180
    assert SEUIL["workoutSegments"][0]["workoutSteps"][3]["workoutSteps"][0]["targetValueOne"] == 163
    assert len(changes) == 4 and "163-172 W -> 171-180 W" in changes[2]


def test_compute_load_steady_state():
    end = date(2026, 11, 13)
    daily = {end - timedelta(days=i): 60.0 for i in range(200)}
    load = compute_load(daily, end)
    assert load["ctl_fitness"] == pytest.approx(60, abs=1) and load["atl_fatigue"] == pytest.approx(60, abs=0.5)
    assert abs(load["tsb_form"]) < 1 and load["acute_chronic_ratio"] == 1.0
    # one big block of 3 days pushes fatigue up and form down
    for i in range(3):
        daily[end - timedelta(days=i)] = 150.0
    tired = compute_load(daily, end)
    assert tired["atl_fatigue"] > tired["ctl_fitness"] and tired["tsb_form"] < -15


def test_daily_tss_estimates_missing_values():
    acts = [{"startTimeLocal": "2026-11-13 12:41:40", "trainingStressScore": 96.4},
            {"startTimeLocal": "2026-11-13 18:00:00", "normPower": 172, "duration": 3600},
            {"startTimeLocal": "bad"}]
    out = daily_tss_from_activities(acts, ftp=172)
    assert out == {date(2026, 11, 13): pytest.approx(196.4)}


def test_parse_training_status_real_shape():
    ts = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
        "sinceDate": "2026-11-05", "weeklyTrainingLoad": 503, "loadTunnelMin": 281, "loadTunnelMax": 640,
        "trainingStatusFeedbackPhrase": "UNPRODUCTIVE_BALANCED", "acuteTrainingLoadDTO": None}}},
        "mostRecentVO2Max": {"cycling": {"vo2MaxPreciseValue": 52.6}}}
    out = parse_training_status(ts)
    assert out["status"] == "UNPRODUCTIVE_BALANCED" and out["optimal_weekly_load_range"] == [281, 640]
    assert out["vo2max_cycling"] == 52.6


def test_exercise_search_and_resolve():
    assert any(h["id"] == "PULL_UP/PULL_UP" for h in search_exercises("pull up"))
    assert resolve_exercise("barbell back squat")["exercise"] == "BARBELL_BACK_SQUAT"
    assert resolve_exercise("SQUAT/BARBELL_BACK_SQUAT")["category"] == "SQUAT"
    with pytest.raises(ValueError, match="Close matches"):
        resolve_exercise("back squatt")


def test_build_strength_workout():
    w = build_strength_workout("Jambes", [
        PlannedExercise(exercise="Barbell Back Squat", sets=4, reps=6, weight_kg=90, rest_s=150),
        PlannedExercise(exercise="Plank", sets=3, duration_s=45, rest_s=60),
    ])
    steps = w["workoutSegments"][0]["workoutSteps"]
    assert w["sportType"]["sportTypeKey"] == "strength_training"
    assert steps[0]["numberOfIterations"] == 4 and steps[0]["workoutSteps"][0]["weightValue"] == 90000
    assert [steps[0]["stepOrder"], steps[1]["stepOrder"]] == [1, 4]
    assert w["estimatedDurationInSecs"] == 4 * (18 + 150) + 3 * (45 + 60)
    with pytest.raises(ValueError):
        PlannedExercise(exercise="Plank", sets=3, rest_s=60)


def test_exercise_sets_roundtrip():
    items = [PerformedExercise(exercise="Barbell Back Squat", rest_s=120,
                               sets=[{"reps": 5, "weight_kg": 90}, {"reps": 5, "weight_kg": 92.5}, {"reps": 4, "weight_kg": 92.5}])]
    payload, dur = exercise_sets_payload(42, datetime(2026, 11, 13, 18, 0), items)
    active = [s for s in payload["exerciseSets"] if s["setType"] == "ACTIVE"]
    assert len(active) == 3 and active[1]["weight"] == 92500 and payload["activityId"] == 42
    assert dur == (5 + 5 + 4) * 3 + 3 * 120
    parsed = parse_exercise_sets(payload)
    assert parsed[0]["exercise"] == "Barbell Back Squat"
    assert parsed[0]["top_set"] == "5 x 92.5 kg" and parsed[0]["volume_kg"] == 450 + 462 + 370
    assert parsed[0]["est_1rm_kg"] == pytest.approx(107.9, abs=0.1)
