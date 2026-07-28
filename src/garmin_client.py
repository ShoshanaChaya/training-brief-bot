"""Garmin Connect data fetcher.

Uses the unofficial python-garminconnect library. Logs in fresh each run
(no token caching across GH Actions invocations — the runner is ephemeral).

If Garmin starts flagging the login, we'd switch to caching `garth` OAuth
tokens as a GH secret, but for daily once-a-day calls fresh login is fine.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any

from garminconnect import Garmin

log = logging.getLogger(__name__)


@dataclass
class GarminSnapshot:
    """Everything we pull from Garmin for one day's briefing."""

    fetched_for: str  # ISO date this snapshot represents (yesterday)
    sleep: dict[str, Any] | None
    body_battery: dict[str, Any] | None
    hrv: dict[str, Any] | None
    stress: dict[str, Any] | None
    recent_activities: list[dict[str, Any]]  # last 7 days
    training_status: dict[str, Any] | None
    error: str | None = None  # populated if anything failed; partial data still usable

    def to_prompt_dict(self) -> dict[str, Any]:
        """Trimmed view passed to Claude (drops noisy raw fields)."""
        return asdict(self)


def fetch_snapshot(email: str, password: str) -> GarminSnapshot:
    """Pull yesterday's sleep + last 7 days of activities + current training status.

    Returns a snapshot with whatever we successfully retrieved. Individual
    field failures don't kill the whole snapshot — Claude can work with partial
    data and will note what's missing.
    """
    today = date.today()
    week_ago = today - timedelta(days=7)
    iso_today = today.isoformat()
    iso_week_ago = week_ago.isoformat()

    # Garmin labels sleep / body battery / HRV by the date the night ENDED.
    # So "today" = last night's sleep. Using yesterday returns the night
    # *before* last night, which confused the briefing badly.

    snapshot = GarminSnapshot(
        fetched_for=iso_today,
        sleep=None,
        body_battery=None,
        hrv=None,
        stress=None,
        recent_activities=[],
        training_status=None,
    )

    try:
        client = Garmin(email, password)
        client.login()
    except Exception as e:
        snapshot.error = f"Garmin login failed: {e}"
        log.exception("Garmin login failed")
        return snapshot

    # Each fetch is wrapped — one failure shouldn't kill the rest.
    def _safe(label: str, fn):
        try:
            return fn()
        except Exception as e:
            log.warning("Garmin fetch failed: %s — %s", label, e)
            return None

    # All "current state" metrics use TODAY — that's last night's sleep,
    # current body battery, today's HRV/stress reading.
    snapshot.sleep = _safe("sleep", lambda: client.get_sleep_data(iso_today))
    snapshot.body_battery = _safe(
        "body_battery",
        lambda: client.get_body_battery(iso_today, iso_today),
    )
    snapshot.hrv = _safe("hrv", lambda: client.get_hrv_data(iso_today))
    snapshot.stress = _safe("stress", lambda: client.get_stress_data(iso_today))
    snapshot.training_status = _safe(
        "training_status", lambda: client.get_training_status(iso_today)
    )

    # Activities — last 7 days, trim to fields the coach actually needs.
    raw_activities = _safe(
        "activities",
        lambda: client.get_activities_by_date(iso_week_ago, iso_today),
    ) or []

    keep_keys = {
        "activityName",
        "activityType",
        "startTimeLocal",
        "duration",
        "distance",
        "averageHR",
        "maxHR",
        "calories",
        "averagePower",
        "normalizedPower",
        "elevationGain",
        "averageRunningCadenceInStepsPerMinute",
        "averageBikingCadenceInRevPerMinute",
        "averageSwimCadenceInStrokesPerMinute",
        "trainingEffect",
        "anaerobicTrainingEffect",
    }
    for act in raw_activities:
        trimmed = {k: v for k, v in act.items() if k in keep_keys}
        # activityType is a nested dict, flatten the typeKey
        atype = act.get("activityType")
        if isinstance(atype, dict):
            trimmed["activityType"] = atype.get("typeKey", str(atype))
        snapshot.recent_activities.append(trimmed)

    return snapshot


def fetch_snapshot_from_env() -> GarminSnapshot:
    """Convenience wrapper that reads creds from env."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "GARMIN_EMAIL and GARMIN_PASSWORD must be set in environment"
        )
    return fetch_snapshot(email, password)
