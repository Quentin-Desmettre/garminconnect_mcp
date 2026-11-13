"""Recovery and training-load helpers (pure functions + Garmin response parsers)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

WEARABLE_NOTE = (
    "Garmin only measures sleep, HRV, resting HR, Body Battery and Training Readiness with a watch worn "
    "day and night. With only a bike computer (e.g. Edge 830) these stay empty: use training load "
    "(Garmin's and the computed ATL/CTL/TSB) and how the user feels instead."
)


def compute_load(daily_tss: dict[date, float], end: date) -> dict[str, Any]:
    """Banister model: CTL (fitness, 42 d), ATL (fatigue, 7 d), TSB = CTL - ATL, as of the end of ``end``.

    Uses exponentially weighted averages over every day from the first one
    with data; days without a session count as 0 TSS.
    """
    if not daily_tss:
        return {"ctl": 0, "atl": 0, "tsb": 0, "days_of_history": 0}
    start = min(daily_tss)
    ctl = atl = 0.0
    k_ctl, k_atl = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    ctl_prev = atl_prev = 0.0
    d = start
    while d <= end:
        ctl_prev, atl_prev = ctl, atl
        tss = daily_tss.get(d, 0.0)
        ctl += (tss - ctl) * k_ctl
        atl += (tss - atl) * k_atl
        d += timedelta(days=1)
    week = [daily_tss.get(end - timedelta(days=i), 0.0) for i in range(7)]
    month = [daily_tss.get(end - timedelta(days=i), 0.0) for i in range(28)]
    chronic_weekly = sum(month) / 4
    return {
        "ctl_fitness": round(ctl, 1),
        "atl_fatigue": round(atl, 1),
        # Form going into the next day = yesterday's CTL - ATL
        "tsb_form": round(ctl - atl, 1),
        "tsb_form_before_last_day": round(ctl_prev - atl_prev, 1),
        "tss_last_7d": round(sum(week)),
        "tss_last_28d_weekly_avg": round(chronic_weekly),
        "acute_chronic_ratio": round(sum(week) / chronic_weekly, 2) if chronic_weekly else None,
        "days_of_history": (end - start).days + 1,
    }


def daily_tss_from_activities(activities: list[dict[str, Any]], *, ftp: int | None = None) -> dict[date, float]:
    """Sum TSS per day; estimate it from NP and duration when Garmin did not compute it."""
    out: dict[date, float] = {}
    for a in activities:
        start = str(a.get("startTimeLocal") or "")[:10]
        try:
            d = date.fromisoformat(start)
        except ValueError:
            continue
        tss = a.get("trainingStressScore")
        if tss is None and ftp and a.get("normPower") and a.get("duration"):
            intensity = a["normPower"] / ftp
            tss = a["duration"] * a["normPower"] * intensity / (ftp * 3600) * 100
        if tss:
            out[d] = out.get(d, 0.0) + float(tss)
    return out


def interpret_tsb(tsb: float) -> str:
    if tsb > 15:
        return "very fresh (possibly losing fitness if this lasts)"
    if tsb > 5:
        return "fresh: good day for a hard session or a test"
    if tsb >= -10:
        return "neutral: normal training"
    if tsb >= -25:
        return "tired (productive overload): hard sessions possible but watch how the legs feel"
    return "very tired: prioritise recovery, move intense sessions"


def parse_training_status(ts: dict[str, Any] | None) -> dict[str, Any]:
    if not ts:
        return {}
    out: dict[str, Any] = {}
    status = next(iter(((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}).values()), None)
    if status:
        acute = status.get("acuteTrainingLoadDTO") or {}
        out["status"] = status.get("trainingStatusFeedbackPhrase")
        out["since"] = status.get("sinceDate")
        out["weekly_load"] = status.get("weeklyTrainingLoad")
        out["optimal_weekly_load_range"] = [status.get("loadTunnelMin"), status.get("loadTunnelMax")]
        if acute:
            out["acute_load"] = acute.get("dailyTrainingLoadAcute")
            out["chronic_load"] = acute.get("dailyTrainingLoadChronic")
            out["acwr"] = acute.get("dailyAcuteChronicWorkloadRatio")
            out["acwr_status"] = acute.get("acwrStatus")
    bal = next(iter(((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}).values()), None)
    if bal:
        out["load_focus_4_weeks"] = {
            "low_aerobic": [round(bal.get("monthlyLoadAerobicLow") or 0), bal.get("monthlyLoadAerobicLowTargetMin"),
                            bal.get("monthlyLoadAerobicLowTargetMax")],
            "high_aerobic": [round(bal.get("monthlyLoadAerobicHigh") or 0), bal.get("monthlyLoadAerobicHighTargetMin"),
                             bal.get("monthlyLoadAerobicHighTargetMax")],
            "anaerobic": [round(bal.get("monthlyLoadAnaerobic") or 0), bal.get("monthlyLoadAnaerobicTargetMin"),
                          bal.get("monthlyLoadAnaerobicTargetMax")],
            "format": "[value, target_min, target_max]",
            "feedback": bal.get("trainingBalanceFeedbackPhrase"),
        }
    vo2 = (ts.get("mostRecentVO2Max") or {}).get("cycling") or {}
    if vo2.get("vo2MaxPreciseValue"):
        out["vo2max_cycling"] = vo2["vo2MaxPreciseValue"]
    return out


def parse_sleep(s: dict[str, Any] | None) -> dict[str, Any] | None:
    d = (s or {}).get("dailySleepDTO") or {}
    if not d.get("sleepTimeSeconds"):
        return None
    out = {
        "sleep_h": round(d["sleepTimeSeconds"] / 3600, 1),
        "deep_min": round((d.get("deepSleepSeconds") or 0) / 60),
        "rem_min": round((d.get("remSleepSeconds") or 0) / 60),
        "awake_min": round((d.get("awakeSleepSeconds") or 0) / 60),
    }
    score = ((d.get("sleepScores") or {}).get("overall") or {}).get("value")
    if score is not None:
        out["score"] = score
    return out


def parse_hrv(h: dict[str, Any] | None) -> dict[str, Any] | None:
    s = (h or {}).get("hrvSummary") or {}
    if not s.get("lastNightAvg"):
        return None
    base = s.get("baseline") or {}
    return {
        "last_night_avg_ms": s.get("lastNightAvg"),
        "weekly_avg_ms": s.get("weeklyAvg"),
        "status": s.get("status"),
        "baseline_ms": [base.get("balancedLow"), base.get("balancedUpper")],
    }


def parse_readiness(r: Any) -> dict[str, Any] | None:
    if isinstance(r, list):
        r = r[0] if r else None
    if not r or r.get("score") is None:
        return None
    return {"score": r.get("score"), "level": r.get("level"), "feedback": r.get("feedbackShort")}


def parse_body_battery(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for row in reversed(rows or []):
        values = [v[1] for v in row.get("bodyBatteryValuesArray") or [] if isinstance(v, list) and len(v) > 1
                  and v[1] is not None]
        if values or row.get("charged"):
            return {"date": row.get("date"), "charged": row.get("charged"), "drained": row.get("drained"),
                    "max": max(values) if values else None, "latest": values[-1] if values else None}
    return None
