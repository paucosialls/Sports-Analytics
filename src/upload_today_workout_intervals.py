#!/usr/bin/env python3
"""Upload today's ZWO workout to Intervals.icu.

Reads:
  out/today_workout.json
  out/today_workout.zwo

Environment:
  INTERVALS_API_KEY      required
  INTERVALS_ATHLETE_ID   optional, defaults to 0
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
WORKOUT_FILES = (
    (OUT / "today_workout.json", OUT / "today_workout.zwo"),
    (OUT / "today_outdoor_workout.json", OUT / "today_outdoor_workout.zwo"),
)
API_BASE = "https://intervals.icu/api/v1"



def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def load_workout(json_path: Path, zwo_path: Path) -> tuple[dict, str]:
    if not json_path.exists() or not zwo_path.exists():
        raise FileNotFoundError(
            "Workout files not found. Run src/generate_today_workout.py first."
        )
    return json.loads(json_path.read_text()), zwo_path.read_text()


def upload(workout: dict, zwo: str, api_key: str, athlete_id: str) -> dict:
    workout_date = workout.get("date")
    name = workout.get("name", "Today indoor workout")
    mode = workout.get("mode", "indoor")
    # Keep the original indoor ID so the first automated run updates the event
    # that older versions of this script already created.
    external_id = (
        f"sports-analytics-today-{workout_date}"
        if mode == "indoor"
        else f"sports-analytics-today-{mode}-{workout_date}"
    )
    payload = [
        {
            "category": "WORKOUT",
            "type": "Ride",
            "name": name,
            "description": workout.get("goal", ""),
            "start_date_local": f"{workout_date}T00:00:00",
            "filename": f"{workout_date}-today-{mode}-workout.zwo",
            "file_contents": zwo,
            "external_id": external_id,
        }
    ]
    url = f"{API_BASE}/athlete/{athlete_id}/events/bulk?upsert=true"
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Sports-Analytics/1.0 (+https://github.com/paucosialls/Sports-Analytics)",
        },
        method="POST",
    )
    import base64
    req.headers["Authorization"] = "Basic " + base64.b64encode(
        f"API_KEY:{api_key}".encode("utf-8")
    ).decode("ascii")

    with request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return {"status": resp.status, "body": json.loads(body) if body else None}


def main() -> int:
    load_env_file(ROOT / ".env")
    api_key = os.getenv("INTERVALS_API_KEY")
    athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0")
    if not api_key:
        return fail("INTERVALS_API_KEY is not set in the environment or .env.")
    try:
        results = []
        for json_path, zwo_path in WORKOUT_FILES:
            workout, zwo = load_workout(json_path, zwo_path)
            results.append((workout, upload(workout, zwo, api_key, athlete_id)))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return fail(f"Intervals upload failed: HTTP {exc.code}: {detail}")
    except Exception as exc:
        return fail(f"Intervals upload failed: {exc}")

    for workout, result in results:
        events = result.get("body") or []
        event = events[0] if isinstance(events, list) and events else {}
        event_id = event.get("id") or event.get("uid") or "unknown"
        print(
            f"Uploaded {workout['date']} {workout.get('mode', 'indoor')} workout "
            f"at {datetime.now().isoformat(timespec='seconds')} (event {event_id})."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
