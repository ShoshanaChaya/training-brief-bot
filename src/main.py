"""Entry point. Run once per day:

    python -m src.main

Or with --dry-run to print the briefing instead of emailing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TARGET_TZ = ZoneInfo("Europe/Zurich")
# Dedupe state — committed back to the repo by the workflow after a successful
# send. Lets us schedule the cron multiple times and still send only once/day.
LAST_SENT_FILE = Path(__file__).resolve().parent.parent / ".last_sent_zurich_date"


def _today_zurich_iso() -> str:
    return datetime.now(TARGET_TZ).date().isoformat()


def _already_sent_today() -> bool:
    if not LAST_SENT_FILE.exists():
        return False
    return LAST_SENT_FILE.read_text().strip() == _today_zurich_iso()


def _mark_sent_today() -> None:
    LAST_SENT_FILE.write_text(_today_zurich_iso() + "\n")

# Load .env if present (local dev). In GH Actions the env is already set.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv is optional

from src import coach, emailer, garmin_client


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily workout coach bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print briefing to stdout instead of emailing",
    )
    parser.add_argument(
        "--skip-garmin",
        action="store_true",
        help="Skip Garmin fetch (use empty snapshot — useful for testing)",
    )
    parser.add_argument(
        "--ignore-dedupe",
        action="store_true",
        help="Send even if today's already been sent (debug only)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("main")

    # Dedupe — schedule fires multiple times per day to be robust against
    # GitHub Actions' chronic cron delays (1-2hr is common). We send once
    # per Zurich-day and ignore subsequent firings. The workflow commits
    # the state file after a successful send so the next run can read it.
    # Manual triggers (workflow_dispatch, local CLI) bypass dedupe.
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "schedule" and not args.ignore_dedupe:
        if _already_sent_today():
            log.info(
                "Already sent for %s (Zurich) — skipping this run.",
                _today_zurich_iso(),
            )
            return 0

    # 1. Pull Garmin data
    if args.skip_garmin:
        log.info("Skipping Garmin fetch (--skip-garmin)")
        snapshot_dict = {"note": "Garmin data skipped for this run"}
    else:
        log.info("Fetching Garmin snapshot")
        snapshot = garmin_client.fetch_snapshot_from_env()
        if snapshot.error:
            log.warning("Garmin had errors: %s", snapshot.error)
        snapshot_dict = snapshot.to_prompt_dict()

    # 2. Generate briefing
    log.info("Generating briefing")
    briefing = coach.generate_briefing(snapshot_dict)

    # 3. Email or print
    if args.dry_run:
        print("=" * 60)
        print(briefing)
        print("=" * 60)
        return 0

    log.info("Sending email")
    emailer.send_briefing(briefing)

    # Mark sent so subsequent same-day cron firings skip. Only for scheduled
    # runs — manual runs intentionally don't update state (so you can re-test
    # without breaking the next morning's send).
    if event == "schedule":
        _mark_sent_today()
        log.info("Marked sent for %s (Zurich)", _today_zurich_iso())

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
