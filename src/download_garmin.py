"""
download_garmin.py — Bulk download all Garmin Connect activities as GPX files.

Usage:
    1. Copy .env.example to .env and fill in your credentials.
    2. Run: python src/download_garmin.py

Resume-safe: already-downloaded files are skipped.
Rate-limited: 1s delay between downloads to avoid API bans.
"""

import os
import sys
import time
import signal
import logging
from pathlib import Path
from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError
from garth.exc import GarthHTTPError
from tqdm import tqdm

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
TOKEN_STORE = Path.home() / ".garth"   # cached OAuth tokens — avoids re-login
PAGE_SIZE = 100          # Garmin API max per request
DELAY_BETWEEN_DL = 0.5  # seconds between each activity download
DELAY_BETWEEN_PAGES = 2.0  # seconds between page fetches
MAX_RETRIES = 3
RETRY_DELAY = 10.0       # seconds to wait after a failed download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_stop = False

def _handle_sigint(sig, frame):
    global _stop
    print("\nInterrupt received — finishing current download then stopping.")
    _stop = True

signal.signal(signal.SIGINT, _handle_sigint)


# ── Auth ──────────────────────────────────────────────────────────────────────
def connect() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        log.error("GARMIN_EMAIL / GARMIN_PASSWORD not set. Copy .env.example to .env and fill them in.")
        sys.exit(1)

    api = Garmin(email, password)

    # Try resuming a cached session first (avoids SSO rate limits)
    if TOKEN_STORE.exists():
        try:
            log.info("Resuming cached Garmin session from %s …", TOKEN_STORE)
            api.login(str(TOKEN_STORE))
            log.info("Session resumed.")
            return api
        except Exception:
            log.info("Cached session expired — doing fresh login.")

    # Fresh login with retry backoff for 429s
    log.info("Logging in to Garmin Connect as %s …", email)
    for attempt in range(1, 4):
        try:
            api.login()
            TOKEN_STORE.mkdir(parents=True, exist_ok=True)
            api.garth.dump(str(TOKEN_STORE))
            log.info("Login successful. Session cached to %s", TOKEN_STORE)
            return api
        except GarthHTTPError as e:
            if "429" in str(e) and attempt < 3:
                wait = 30 * attempt
                log.warning("Rate limited by Garmin SSO (429). Waiting %ds before retry %d/3 …", wait, attempt)
                time.sleep(wait)
            else:
                log.error("Login failed: %s", e)
                sys.exit(1)
        except GarminConnectAuthenticationError as e:
            log.error("Authentication failed: %s", e)
            sys.exit(1)


# ── Activity list ─────────────────────────────────────────────────────────────
def fetch_all_activity_ids(api: Garmin) -> list[dict]:
    """Page through the full activity list and return all activity dicts."""
    all_activities = []
    start = 0
    log.info("Fetching activity list …")
    while True:
        batch = api.get_activities(start=start, limit=PAGE_SIZE)
        if not batch:
            break
        all_activities.extend(batch)
        log.info("  fetched %d/%d+ activities", len(all_activities), len(all_activities))
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(DELAY_BETWEEN_PAGES)
    log.info("Total activities found: %d", len(all_activities))
    return all_activities


# ── Download ──────────────────────────────────────────────────────────────────
def download_activity_gpx(api: Garmin, activity: dict, dest_dir: Path) -> bool:
    """Download a single activity as GPX. Returns True on success."""
    activity_id = activity["activityId"]
    activity_name = activity.get("activityName", "unknown").replace("/", "-")
    start_time = activity.get("startTimeLocal", "")[:10]  # YYYY-MM-DD
    filename = f"{start_time}_{activity_id}_{activity_name[:40]}.gpx"
    dest = dest_dir / filename

    if dest.exists():
        return True  # already downloaded

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            gpx_data = api.download_activity(
                activity_id,
                dl_fmt=api.ActivityDownloadFormat.GPX,
            )
            dest.write_bytes(gpx_data)
            return True
        except Exception as e:
            log.warning(
                "  [%d/%d] Failed to download %s: %s",
                attempt, MAX_RETRIES, activity_id, e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    log.error("  Giving up on activity %s after %d attempts.", activity_id, MAX_RETRIES)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    api = connect()
    activities = fetch_all_activity_ids(api)

    already_downloaded = len(list(RAW_DIR.glob("*.gpx")))
    to_download = [
        a for a in activities
        if not any(RAW_DIR.glob(f"*_{a['activityId']}_*.gpx"))
    ]

    log.info(
        "%d already on disk, %d to download.",
        already_downloaded, len(to_download),
    )

    ok = fail = 0
    with tqdm(total=len(to_download), unit="activity") as bar:
        for activity in to_download:
            if _stop:
                break
            success = download_activity_gpx(api, activity, RAW_DIR)
            if success:
                ok += 1
            else:
                fail += 1
            bar.update(1)
            bar.set_postfix(ok=ok, fail=fail)
            time.sleep(DELAY_BETWEEN_DL)

    log.info("Done. Downloaded: %d  Failed: %d  Skipped/existing: %d", ok, fail, already_downloaded)


if __name__ == "__main__":
    main()
