"""
fetch_daily_stats.py — Pull daily wellness stats from Garmin Connect.

For each day from the earliest activity onward, saves:
  totalKilocalories, bmrKilocalories, activeKilocalories,
  restingHeartRate, totalDistanceMeters, moderateIntensityMinutes,
  vigorousIntensityMinutes, sleepingSeconds, averageStressLevel.

Resume-safe: already-fetched dates are skipped.
Rate-limited: small delay between requests.

Also writes dashboard/daily_stats.js for the "Understanding Myself" page.
"""
from __future__ import annotations

import os
import sys
import time
import json
import signal
import logging
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError
from garth.exc import GarthHTTPError
from tqdm import tqdm

load_dotenv()

ROOT = Path(__file__).parent.parent
OUT_CSV = ROOT / "data" / "processed" / "daily_stats.csv"
OUT_JS  = ROOT / "dashboard" / "daily_stats.js"
TOKEN_STORE = Path.home() / ".garth"
ACTIVITIES_CSV = ROOT / "data" / "processed" / "activities.csv"

DELAY = 0.6          # seconds between requests
PAGE_DELAY = 1.5
MAX_RETRIES = 3
RETRY_DELAY = 10.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_stop = False
def _handle_sigint(sig, frame):
    global _stop
    print("\nInterrupt received — finishing current request then stopping.")
    _stop = True
signal.signal(signal.SIGINT, _handle_sigint)


FIELDS = [
    'calendarDate',
    'totalKilocalories',
    'bmrKilocalories',
    'activeKilocalories',
    'restingHeartRate',
    'totalDistanceMeters',
    'moderateIntensityMinutes',
    'vigorousIntensityMinutes',
    'sleepingSeconds',
    'averageStressLevel',
    'maxHeartRate',
    'minHeartRate',
    'totalSteps',
    'floorsAscended',
]


def connect() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        log.error("GARMIN_EMAIL / GARMIN_PASSWORD not set.")
        sys.exit(1)
    api = Garmin(email, password)
    if TOKEN_STORE.exists():
        try:
            log.info("Resuming Garmin session from %s", TOKEN_STORE)
            api.login(str(TOKEN_STORE))
            return api
        except Exception:
            log.info("Cached session expired — fresh login.")
    for attempt in range(1, 4):
        try:
            api.login()
            TOKEN_STORE.mkdir(parents=True, exist_ok=True)
            api.garth.dump(str(TOKEN_STORE))
            log.info("Login ok; session cached.")
            return api
        except GarthHTTPError as e:
            if "429" in str(e) and attempt < 3:
                wait = 30 * attempt
                log.warning("429 — backing off %ds", wait)
                time.sleep(wait)
            else:
                log.error("Login failed: %s", e)
                sys.exit(1)
        except GarminConnectAuthenticationError as e:
            log.error("Auth failed: %s", e)
            sys.exit(1)


def load_existing() -> set[str]:
    if not OUT_CSV.exists():
        return set()
    try:
        df = pd.read_csv(OUT_CSV, usecols=['calendarDate'])
        return set(df['calendarDate'].astype(str))
    except Exception:
        return set()


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def earliest_activity_date() -> date:
    if not ACTIVITIES_CSV.exists():
        return date(2017, 1, 1)
    df = pd.read_csv(ACTIVITIES_CSV, usecols=['date'])
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df['date'].min()


def fetch_one(api: Garmin, d: date) -> dict | None:
    s = d.isoformat()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = api.get_stats(s)
            if not payload:
                return None
            row = {k: payload.get(k) for k in FIELDS}
            row['calendarDate'] = s
            return row
        except GarthHTTPError as e:
            if "429" in str(e) and attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
                continue
            log.warning("Failed %s: %s", s, e)
            return None
        except Exception as e:
            log.warning("Failed %s: %s", s, e)
            return None


def flush(rows: list[dict]):
    if not rows:
        return
    new_df = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if OUT_CSV.exists():
        old = pd.read_csv(OUT_CSV)
        combined = pd.concat([old, new_df], ignore_index=True)
        combined = combined.drop_duplicates('calendarDate', keep='last')
        combined = combined.sort_values('calendarDate')
        combined.to_csv(OUT_CSV, index=False)
    else:
        new_df.sort_values('calendarDate').to_csv(OUT_CSV, index=False)


def main():
    # --since YYYY-MM-DD limits the start date (useful for quick backfill)
    since = None
    for i, a in enumerate(sys.argv):
        if a == '--since' and i + 1 < len(sys.argv):
            since = date.fromisoformat(sys.argv[i + 1])

    api = connect()

    start = max(earliest_activity_date(), since) if since else earliest_activity_date()
    end = date.today()
    all_days = list(daterange(start, end))
    existing = load_existing()
    missing = [d for d in all_days if d.isoformat() not in existing]

    log.info("Range: %s → %s (%d days).  Already fetched: %d.  To fetch: %d.",
             start, end, len(all_days), len(existing), len(missing))

    if not missing:
        log.info("All days up to date.")
        _write_js()
        return

    rows = []
    FLUSH_EVERY = 30
    try:
        for d in tqdm(missing, unit='day'):
            if _stop:
                break
            row = fetch_one(api, d)
            if row:
                rows.append(row)
            if len(rows) >= FLUSH_EVERY:
                flush(rows)
                rows = []
            time.sleep(DELAY)
    finally:
        flush(rows)
        log.info("Wrote rows → %s", OUT_CSV)
        _write_js()


def _write_js():
    """Emit dashboard/daily_stats.js with the full CSV as JSON."""
    if not OUT_CSV.exists():
        return
    df = pd.read_csv(OUT_CSV)
    df = df.where(pd.notna(df), None)  # NaN → None for JSON
    records = df.to_dict(orient='records')
    OUT_JS.parent.mkdir(parents=True, exist_ok=True)
    OUT_JS.write_text("window.DAILY_STATS = " + json.dumps(records) + ";\n")
    log.info("Wrote JS → %s  (%d rows)", OUT_JS, len(records))


if __name__ == '__main__':
    main()
