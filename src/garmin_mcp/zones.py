"""Pure helpers to recompute Garmin power and heart-rate zones."""

from __future__ import annotations

import copy
from typing import Any, Literal

POWER_ZONE_COUNT = 7
HR_ZONE_COUNT = 5

# Coggan zone floors in % FTP (Z1..Z7)
COGGAN_FLOORS_PCT = [0, 56, 76, 91, 106, 121, 151]

PowerZoneMode = Literal["keep_percentages", "coggan", "unchanged"]


def _floors(obj: dict[str, Any], count: int) -> list[float | None]:
    return [obj.get(f"zone{i}Floor") for i in range(1, count + 1)]


def _set_floors(obj: dict[str, Any], floors: list[float]) -> None:
    for i, value in enumerate(floors, start=1):
        obj[f"zone{i}Floor"] = int(round(value))


def _check_ascending(floors: list[float], label: str) -> None:
    if any(b <= a for a, b in zip(floors, floors[1:], strict=False)):
        raise ValueError(f"{label} zone floors must be strictly increasing: {floors}")


def find_sport(objs: list[dict[str, Any]], sport: str) -> dict[str, Any]:
    wanted = sport.strip().upper()
    for obj in objs:
        if str(obj.get("sport", "")).upper() == wanted:
            return obj
    available = sorted({str(o.get("sport")) for o in objs})
    raise ValueError(f"sport {sport!r} not found; available: {available}")


def updated_power_zones(
    current: dict[str, Any],
    *,
    ftp: int | None = None,
    mode: PowerZoneMode = "keep_percentages",
    floors_watts: list[float] | None = None,
) -> dict[str, Any]:
    """Return a copy of a power-zone object with a new FTP and/or new zone floors."""
    obj = copy.deepcopy(current)
    old_ftp = obj.get("functionalThresholdPower")
    new_ftp = ftp if ftp is not None else old_ftp
    if ftp is not None:
        if not 50 <= ftp <= 700:
            raise ValueError("FTP must be between 50 and 700 W")
        obj["functionalThresholdPower"] = ftp

    if floors_watts is not None:
        if len(floors_watts) != POWER_ZONE_COUNT:
            raise ValueError(f"expected {POWER_ZONE_COUNT} power zone floors, got {len(floors_watts)}")
        _check_ascending(floors_watts, "power")
        _set_floors(obj, floors_watts)
        return obj

    if mode == "unchanged" or ftp is None:
        return obj
    if mode == "coggan":
        _set_floors(obj, [new_ftp * p / 100 for p in COGGAN_FLOORS_PCT])
        return obj
    # keep_percentages: scale every floor by new/old FTP
    floors = _floors(obj, POWER_ZONE_COUNT)
    if not old_ftp or any(f is None for f in floors):
        _set_floors(obj, [new_ftp * p / 100 for p in COGGAN_FLOORS_PCT])
    else:
        ratio = new_ftp / old_ftp
        _set_floors(obj, [f * ratio for f in floors])  # type: ignore[operator]
    return obj


def updated_hr_zones(
    current: dict[str, Any],
    *,
    training_method: Literal["HR_MAX", "HR_RESERVE", "LACTATE_THRESHOLD"] | None = None,
    max_hr: int | None = None,
    resting_hr: int | None = None,
    lthr: int | None = None,
    floors_bpm: list[float] | None = None,
) -> dict[str, Any]:
    """Return a copy of an HR-zone object with new reference values / floors.

    When reference values change but no floors are given, the floors are
    rescaled so each zone keeps the same relative position for the method
    in use (% max HR, % HR reserve or % LTHR).
    """
    obj = copy.deepcopy(current)
    for value, lo, hi, label in (
        (max_hr, 120, 230, "max_hr"),
        (resting_hr, 25, 110, "resting_hr"),
        (lthr, 100, 220, "lthr"),
    ):
        if value is not None and not lo <= value <= hi:
            raise ValueError(f"{label} must be between {lo} and {hi} bpm")

    old_max = obj.get("maxHeartRateUsed")
    old_rest = obj.get("restingHeartRateUsed")
    old_lt = obj.get("lactateThresholdHeartRateUsed")
    if training_method:
        obj["trainingMethod"] = training_method
    if max_hr is not None:
        obj["maxHeartRateUsed"] = max_hr
    if resting_hr is not None:
        obj["restingHeartRateUsed"] = resting_hr
    if lthr is not None:
        obj["lactateThresholdHeartRateUsed"] = lthr

    if floors_bpm is not None:
        if len(floors_bpm) != HR_ZONE_COUNT:
            raise ValueError(f"expected {HR_ZONE_COUNT} HR zone floors, got {len(floors_bpm)}")
        _check_ascending(floors_bpm, "heart-rate")
        _set_floors(obj, floors_bpm)
        return obj

    floors = _floors(obj, HR_ZONE_COUNT)
    if any(f is None for f in floors):
        return obj
    method = obj.get("trainingMethod")
    new_max = obj.get("maxHeartRateUsed")
    new_rest = obj.get("restingHeartRateUsed")
    new_lt = obj.get("lactateThresholdHeartRateUsed")

    if method == "HR_MAX" and old_max and new_max and new_max != old_max:
        _set_floors(obj, [f * new_max / old_max for f in floors])  # type: ignore[operator]
    elif (
        method == "HR_RESERVE"
        and old_max and old_rest is not None and new_max and new_rest is not None
        and (new_max, new_rest) != (old_max, old_rest)
        and old_max > old_rest
    ):
        pcts = [(f - old_rest) / (old_max - old_rest) for f in floors]  # type: ignore[operator]
        _set_floors(obj, [new_rest + p * (new_max - new_rest) for p in pcts])
    elif method == "LACTATE_THRESHOLD" and old_lt and new_lt and new_lt != old_lt:
        _set_floors(obj, [f * new_lt / old_lt for f in floors])  # type: ignore[operator]
    return obj


def describe_power_zones(obj: dict[str, Any]) -> dict[str, Any]:
    ftp = obj.get("functionalThresholdPower")
    floors = _floors(obj, POWER_ZONE_COUNT)
    zones = []
    for i, lo in enumerate(floors, start=1):
        hi = floors[i] if i < len(floors) else None
        z: dict[str, Any] = {"zone": i, "from_w": lo, "to_w": (hi - 1) if hi else None}
        if ftp and lo is not None:
            z["from_pct_ftp"] = round(100 * lo / ftp)
        zones.append(z)
    return {"sport": obj.get("sport"), "ftp_w": ftp, "zones": zones}


def describe_hr_zones(obj: dict[str, Any]) -> dict[str, Any]:
    floors = _floors(obj, HR_ZONE_COUNT)
    max_hr = obj.get("maxHeartRateUsed")
    zones = []
    for i, lo in enumerate(floors, start=1):
        hi = floors[i] - 1 if i < len(floors) and floors[i] else max_hr
        zones.append({"zone": i, "from_bpm": lo, "to_bpm": hi})
    return {
        "sport": obj.get("sport"),
        "training_method": obj.get("trainingMethod"),
        "max_hr": max_hr,
        "resting_hr": obj.get("restingHeartRateUsed"),
        "lthr": obj.get("lactateThresholdHeartRateUsed"),
        "zones": zones,
    }
