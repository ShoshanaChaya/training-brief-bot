"""Gemini API call that generates the daily coaching briefing.

Uses Google's Gemini API (free tier — get a key at
https://aistudio.google.com/app/apikey). Model: gemini-2.5-flash.

(gemini-2.5-pro looks tempting but its free tier is now 0 req/day; -flash
remains free at ~1500 req/day and we use 1/day.)

A post-processing scrubber removes any line that mentions a missing Garmin
field alongside a number, as a defense against the model hallucinating
physiological values when Garmin returns partial data. This is the
load-bearing safety net — the model itself can and will fabricate.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from google import genai
from google.genai import types

# Use Zurich for date / day-of-week so "today" matches Yuval's local life,
# not the GitHub runner's UTC clock (matters near midnight).
LOCAL_TZ = ZoneInfo("Europe/Zurich")

log = logging.getLogger(__name__)

# gemini-2.5-flash — the only Gemini model with a usable free tier. Pro's
# free quota is 0, so flash + the post-processing scrubber is what we use.
MODEL = "gemini-2.5-flash"
MAX_OUTPUT_TOKENS = 4000  # briefing is ~600 words, plenty of headroom

# Post-processing: even with explicit "do not fabricate" instructions, models
# sometimes hallucinate physiological numbers. These patterns catch claims
# about specific Garmin fields. If the model mentions one of these AND the
# corresponding field wasn't in our input snapshot, we strip the line.
_FABRICATION_KEYWORDS = {
    "sleep": ("sleep", "slept", "hours of rest"),
    "body_battery": ("body battery", "battery"),
    "hrv": ("hrv", "heart rate variability"),
    "stress": ("stress level", "stress score"),
    "training_status": ("training status", "training load", "vo2 max", "fitness age"),
}


def _scrub_fabrications(briefing_text: str, missing_fields: set[str]) -> str:
    """Strip any line that mentions a missing Garmin field along with a number.

    The combination of (keyword for missing field) + (a digit in the line) is
    a strong signal that the model fabricated a value. Lines that pass through
    are unchanged.
    """
    if not missing_fields:
        return briefing_text

    bad_keywords: list[str] = []
    for field in missing_fields:
        bad_keywords.extend(_FABRICATION_KEYWORDS.get(field, ()))

    has_digit = re.compile(r"\d")
    kept: list[str] = []
    stripped: list[str] = []

    for line in briefing_text.split("\n"):
        line_lower = line.lower()
        line_has_keyword = any(kw in line_lower for kw in bad_keywords)
        if line_has_keyword and has_digit.search(line):
            stripped.append(line.strip())
            continue
        kept.append(line)

    cleaned = "\n".join(kept)
    if stripped:
        log.warning(
            "Scrubber removed %d hallucinated line(s): %s",
            len(stripped),
            stripped,
        )
        cleaned += (
            f"\n\n[Bot note: {len(stripped)} line(s) containing fabricated "
            "Garmin data were removed by the safety filter — Garmin didn't "
            "return that data this run.]"
        )
    return cleaned


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
BASELINE_PATH = Path(__file__).resolve().parent.parent / "baseline.yaml"


def _strip_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop None, empty list, empty dict, and empty string fields.

    Critical for preventing Gemini from hallucinating values into null slots —
    if `"sleep": null` is in the JSON, the model fills it. If `sleep` isn't in
    the JSON at all, it has no slot to fill.
    """
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if v is None or v == [] or v == {} or v == "":
            continue
        if isinstance(v, dict):
            cleaned = _strip_empty(v)
            if cleaned:
                out[k] = cleaned
        elif isinstance(v, list):
            cleaned_items = [_strip_empty(x) if isinstance(x, dict) else x for x in v]
            cleaned_items = [x for x in cleaned_items if x not in (None, {}, [], "")]
            if cleaned_items:
                out[k] = cleaned_items
        else:
            out[k] = v
    return out


def _load_baseline() -> dict[str, Any]:
    with BASELINE_PATH.open() as f:
        return yaml.safe_load(f)


def _todays_plan_block(baseline: dict[str, Any], today_local: datetime) -> str:
    """Render today's training plan as plain text, ready to paste into the
    prompt. Gemini gets the exact day-of-week, the rest day, and an
    unambiguous list of today's sessions — no YAML lookup required.
    """
    day_name = calendar.day_name[today_local.weekday()]  # "Tuesday", etc.
    schedule = baseline.get("weekly_schedule", {}) or {}
    sport_plan = schedule.get("sport_plan", {}) or {}
    rest_day = schedule.get("rest_day", "")

    today_sessions = sport_plan.get(day_name)

    lines = [f"Day of week: {day_name}"]
    if rest_day:
        lines.append(f"Rest day for the week: {rest_day}")

    if today_sessions is None:
        lines.append(
            f"No sport plan entry for {day_name}. Use the athlete's overall "
            "schedule and recovery state to pick a sensible session."
        )
    elif today_sessions == []:
        lines.append(
            f"{day_name} is a REST DAY. Today's session is FULL REST. "
            "Do not prescribe any training (light walking / stretching only "
            "if the athlete asks)."
        )
    else:
        lines.append(f"Today's required sports (from sport_plan, in order):")
        for i, session in enumerate(today_sessions, 1):
            sport = session.get("sport", "?")
            is_long = bool(session.get("long"))
            tag = " — LONG SESSION" if is_long else ""
            lines.append(f"  {i}. {sport}{tag}")
        lines.append(
            "You MUST prescribe these sports today. You may shorten, "
            "lower intensity, or drop one if recovery requires it (and "
            "say so under 'Why'). You MAY NOT substitute a different sport "
            "(e.g. do not swap bike → swim). You MAY NOT add sports that "
            "aren't listed here."
        )

    return "\n".join(lines)


def _load_system_prompt() -> str:
    return (PROMPTS_DIR / "coach_system.md").read_text()


def generate_briefing(garmin_snapshot: dict[str, Any]) -> str:
    """Call Gemini and return the briefing text.

    Args:
        garmin_snapshot: dict from GarminSnapshot.to_prompt_dict().

    Returns:
        Plain-text briefing body, ready to email.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    client = genai.Client(api_key=api_key)

    system_prompt = _load_system_prompt()
    baseline = _load_baseline()
    today_local = datetime.now(LOCAL_TZ)
    today_iso = today_local.date().isoformat()
    today_day_name = calendar.day_name[today_local.weekday()]
    todays_plan_block = _todays_plan_block(baseline, today_local)

    # Combine the static coach instructions with the athlete profile.
    # Both go into Gemini's system_instruction — they don't change between runs.
    full_system = (
        system_prompt
        + "\n\n## Athlete Profile (from baseline.yaml)\n\n"
        + yaml.safe_dump(baseline, sort_keys=True, default_flow_style=False)
    )

    # Detect whether Garmin actually returned anything useful.
    # If login failed or every data field is empty, we deliberately do NOT
    # send any data block to Gemini — empirically, Gemini-2.5-flash will
    # hallucinate sleep/body-battery/training-status numbers if given a
    # null-filled snapshot, even with explicit "do not fabricate" instructions.
    # The only reliable defense is to omit the data entirely.
    garmin_available = (
        not garmin_snapshot.get("error")
        and any(
            garmin_snapshot.get(k)
            for k in ("sleep", "body_battery", "hrv", "stress", "training_status")
        )
        or bool(garmin_snapshot.get("recent_activities"))
    )

    # Track missing fields so the post-processing scrubber knows what to look for.
    all_garmin_fields = (
        "sleep", "body_battery", "hrv", "stress",
        "training_status", "recent_activities",
    )

    if garmin_available:
        # Strip null/empty fields BEFORE sending to Gemini. If we leave
        # `"sleep": null` in the JSON, Gemini will hallucinate a number to
        # fill the slot. If the field isn't in the JSON at all, it can't.
        clean_snapshot = _strip_empty(garmin_snapshot)
        present_fields = sorted(k for k in all_garmin_fields if k in clean_snapshot)
        missing_fields_set = {k for k in all_garmin_fields if k not in clean_snapshot}
        missing_fields = sorted(missing_fields_set)
        missing_note = ""
        if missing_fields:
            missing_note = (
                f"\nNOTE: Garmin returned NO data for: {', '.join(missing_fields)}.\n"
                f"You have ZERO information about those things. DO NOT mention them, "
                f"do not invent values, do not infer them. Only use the fields present "
                f"below: {', '.join(present_fields) or 'none'}.\n"
            )
        user_text = (
            f"<context>\nToday: {today_iso} ({today_day_name})\n</context>\n\n"
            f"<todays_plan>\n{todays_plan_block}\n</todays_plan>\n\n"
            f"<garmin_data>{missing_note}\n"
            f"{json.dumps(clean_snapshot, indent=2, default=str)}\n"
            f"</garmin_data>\n\n"
            "Generate today's briefing. Follow the format in your system prompt exactly."
        )
    else:
        # All Garmin fields are missing — pass full set to the scrubber.
        missing_fields_set = set(all_garmin_fields)
        # No data at all — make the absence loud and unambiguous, and do NOT
        # show Gemini any null fields it could fill in.
        user_text = (
            f"<context>\nToday: {today_iso} ({today_day_name})\n</context>\n\n"
            f"<todays_plan>\n{todays_plan_block}\n</todays_plan>\n\n"
            "<garmin_data>\n"
            "  *** NO DATA FETCHED. ***\n"
            "  Garmin login failed for this run. There is ZERO physiological data\n"
            "  for this athlete today: no sleep, no HRV, no Body Battery, no stress,\n"
            "  no training status, no recent activities.\n"
            "</garmin_data>\n\n"
            "INSTRUCTIONS FOR THIS BRIEFING:\n"
            "1. Use the sports listed in <todays_plan> above. Do NOT swap to other sports.\n"
            "2. You have NO Garmin data. Do not invent any.\n"
            "3. Do NOT mention sleep duration, Body Battery values, HRV, training\n"
            "   status, or any specific recent workout. You have no information about\n"
            "   any of those things. Inventing numbers is a critical failure.\n"
            "4. Under 'Flags', the FIRST bullet must be exactly:\n"
            "   'Garmin data unavailable today; today's plan is based on the profile\n"
            "   and race timing only.'\n"
            "5. Keep the Ironman-readiness flag if the profile fitness is below race\n"
            "   demands.\n"
            "Follow the format in your system prompt exactly."
        )

    log.info("Calling Gemini (%s)", MODEL)

    response = client.models.generate_content(
        model=MODEL,
        contents=user_text,
        config=types.GenerateContentConfig(
            system_instruction=full_system,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.7,  # mild variety so daily briefings don't feel templated
        ),
    )

    # Log token usage if available — useful for staying under free-tier limits.
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        log.info(
            "Tokens — input: %s, output: %s, total: %s",
            getattr(usage, "prompt_token_count", "?"),
            getattr(usage, "candidates_token_count", "?"),
            getattr(usage, "total_token_count", "?"),
        )

    text = (response.text or "").strip()
    if not text:
        # Surface enough info to debug if Gemini blocked the response or returned empty.
        finish_reason = None
        if response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        raise RuntimeError(
            f"Gemini returned no text. finish_reason={finish_reason}"
        )

    # Belt-and-suspenders: strip any lines that mention a missing Garmin field
    # alongside a number — that's the signature of a hallucinated value.
    text = _scrub_fabrications(text, missing_fields_set)
    return text
