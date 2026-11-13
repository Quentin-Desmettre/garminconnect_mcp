import pytest
from pydantic import TypeAdapter, ValidationError

from garmin_mcp.workouts import WorkoutItem, build_cycling_workout, summarize_steps
from garmin_mcp.zones import updated_hr_zones, updated_power_zones

items_adapter = TypeAdapter(list[WorkoutItem])

SWEET_SPOT = [
    {"type": "warmup", "duration_s": 900, "target": {"kind": "power_pct_ftp", "low": 50, "high": 65}},
    {
        "type": "repeat",
        "iterations": 4,
        "steps": [
            {"type": "interval", "duration_s": 480, "target": {"kind": "power_pct_ftp", "low": 88, "high": 94},
             "cadence": {"kind": "cadence", "low": 85, "high": 95}},
            {"type": "recovery", "duration_s": 240, "target": {"kind": "power_zone", "zone": 1}},
        ],
    },
    {"type": "cooldown", "duration_s": 600, "target": {"kind": "hr_zone", "zone": 2}},
    {"type": "other", "description": "free spin"},
]


def test_build_sweet_spot():
    items = items_adapter.validate_python(SWEET_SPOT)
    w = build_cycling_workout("SST 4x8", items, ftp=250)
    assert w["sportType"]["sportTypeKey"] == "cycling"
    assert w["estimatedDurationInSecs"] == 900 + 4 * (480 + 240) + 600
    steps = w["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5, 6]
    warm = steps[0]
    assert warm["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert (warm["targetValueOne"], warm["targetValueTwo"]) == (125.0, 162.0)
    rep = steps[1]
    assert rep["type"] == "RepeatGroupDTO" and rep["numberOfIterations"] == 4
    inner = rep["workoutSteps"]
    assert [s["stepOrder"] for s in inner] == [3, 4]
    assert all(s["childStepId"] == rep["childStepId"] for s in inner)
    assert (inner[0]["targetValueOne"], inner[0]["targetValueTwo"]) == (220.0, 235.0)
    assert inner[0]["secondaryTargetType"]["workoutTargetTypeKey"] == "cadence"
    assert inner[0]["secondaryTargetValueOne"] == 85
    assert inner[1]["zoneNumber"] == 1
    assert steps[2]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert steps[3]["endCondition"]["conditionTypeKey"] == "lap.button"
    lines = summarize_steps(steps)
    assert lines[0] == "- warmup 15:00 | power 125-162 W"
    assert lines[1] == "4x:"
    assert "cadence 85-95 rpm" in lines[2]


def test_pct_ftp_without_ftp_fails():
    items = items_adapter.validate_python(SWEET_SPOT)
    with pytest.raises(ValueError, match="FTP"):
        build_cycling_workout("x", items, ftp=None)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "interval", "duration_s": 60, "distance_m": 100},
        {"type": "interval", "target": {"kind": "power_zone", "zone": 9}},
        {"type": "interval", "target": {"kind": "power_watts", "low": 300, "high": 200}},
        {"type": "interval", "target": {"kind": "hr_bpm", "low": 150}},
        {"type": "sprint"},
    ],
)
def test_invalid_steps(bad):
    with pytest.raises(ValidationError):
        items_adapter.validate_python([bad])


POWER = {"sport": "CYCLING", "functionalThresholdPower": 200,
         "zone1Floor": 0, "zone2Floor": 110, "zone3Floor": 150, "zone4Floor": 180,
         "zone5Floor": 210, "zone6Floor": 240, "zone7Floor": 300, "extra": "kept"}


def test_power_keep_percentages():
    new = updated_power_zones(POWER, ftp=250)
    assert new["functionalThresholdPower"] == 250
    assert new["zone2Floor"] == 138 and new["zone7Floor"] == 375
    assert new["extra"] == "kept" and POWER["functionalThresholdPower"] == 200


def test_power_coggan_and_custom():
    new = updated_power_zones(POWER, ftp=300, mode="coggan")
    assert [new[f"zone{i}Floor"] for i in range(1, 8)] == [0, 168, 228, 273, 318, 363, 453]
    new = updated_power_zones(POWER, floors_watts=[0, 1, 2, 3, 4, 5, 6])
    assert new["zone7Floor"] == 6 and new["functionalThresholdPower"] == 200
    with pytest.raises(ValueError):
        updated_power_zones(POWER, floors_watts=[0, 5, 2, 3, 4, 5, 6])


HR = {"sport": "DEFAULT", "trainingMethod": "HR_RESERVE", "maxHeartRateUsed": 190,
      "restingHeartRateUsed": 50, "lactateThresholdHeartRateUsed": 170,
      "zone1Floor": 120, "zone2Floor": 134, "zone3Floor": 148, "zone4Floor": 162, "zone5Floor": 176}


def test_hr_reserve_rescale():
    new = updated_hr_zones(HR, max_hr=200, resting_hr=45)
    # zone1 was 50% of reserve: 45 + 0.5 * 155
    assert new["zone1Floor"] == 122
    assert new["maxHeartRateUsed"] == 200


def test_hr_explicit_floors_and_bounds():
    new = updated_hr_zones(HR, training_method="LACTATE_THRESHOLD", floors_bpm=[110, 130, 145, 160, 172])
    assert new["trainingMethod"] == "LACTATE_THRESHOLD" and new["zone5Floor"] == 172
    with pytest.raises(ValueError):
        updated_hr_zones(HR, max_hr=300)
