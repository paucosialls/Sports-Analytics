#!/usr/bin/env python3
"""Publish today's workouts once Garmin has supplied post-sleep data."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HEALTH_CSV = ROOT / "data" / "processed" / "health_daily.csv"
STATE_DIR = ROOT / "logs" / "workouts_published"
REQUIRED_WAKE_FIELDS = (
    "sleep_total_s",
    "hrv_last_night_avg",
    "tr_score",
)


def latest_health_row() -> dict[str, str]:
    if not HEALTH_CSV.exists():
        return {}
    with HEALTH_CSV.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-1] if rows else {}


def has_post_sleep_data(row: dict[str, str], today: str) -> bool:
    return row.get("date") == today and all(
        row.get(field) not in (None, "", "null") for field in REQUIRED_WAKE_FIELDS
    )


def main() -> int:
    today = date.today().isoformat()
    marker = STATE_DIR / f"{today}.json"
    if marker.exists():
        print(f"Workouts already published for {today}; skipping.")
        return 0

    health = latest_health_row()
    if not has_post_sleep_data(health, today):
        missing = [
            field
            for field in REQUIRED_WAKE_FIELDS
            if health.get(field) in (None, "", "null")
        ]
        print(
            f"Garmin post-sleep data is not ready for {today}; "
            f"waiting for {', '.join(missing) or 'today row'}."
        )
        return 0

    subprocess.run(
        [sys.executable, str(ROOT / "src" / "generate_today_workout.py")],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "src" / "upload_today_workout_intervals.py")],
        cwd=ROOT,
        check=True,
    )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"date": today, "published": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Marked {today} workouts as published after wake data became available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
