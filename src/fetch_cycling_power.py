"""
fetch_cycling_power.py — Extract per-second power streams from Garmin cycling activities.

For each cycling activity, calls the JSON details endpoint and saves the
per-sample time series (time, power, heart_rate, cadence, speed, distance,
elevation, lat, lon, temperature, power_balance) to
data/processed/cycling_power/<activity_id>.parquet.

Also writes a summary row per activity to data/processed/cycling_activities.csv.

Resume-safe: already-processed activities are skipped.
Rate-limited: delay between requests to avoid bans.
"""
from __future__ import annotations

import os
import sys
import time
import signal
import logging
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError
from garth.exc import GarthHTTPError
from tqdm import tqdm

load_dotenv()

ROOT = Path(__file__).parent.parent
PROCESSED_DIR = ROOT / "data" / "processed" / "cycling_power"
SUMMARY_CSV = ROOT / "data" / "processed" / "cycling_activities.csv"
DASHBOARD_JS = ROOT / "dashboard" / "cycling_activities.js"
TOKEN_STORE = Path.home() / ".garminconnect"

PAGE_SIZE = 100
DELAY_BETWEEN_DL = 0.8
DELAY_BETWEEN_PAGES = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 10.0

# Map Garmin metric descriptor keys → our column names
METRIC_MAP = {
    "sumElapsedDuration": "elapsed_s",
    "sumDuration": "duration_s",
    "sumDistance": "distance_m",
    "sumMovingDuration": "moving_s",
    "directPower": "power_w",
    "directHeartRate": "heart_rate_bpm",
    "directBikeCadence": "cadence_rpm",
    "directSpeed": "speed_mps",
    "directElevation": "elevation_m",
    "directLatitude": "lat",
    "directLongitude": "lon",
    "directAirTemperature": "temperature_c",
    "directPowerBalance": "power_balance_pct",
    "directLeftPowerPhase": "left_power_phase",
    "directRightPowerPhase": "right_power_phase",
    "directLeftTorqueEffectiveness": "left_torque_eff_pct",
    "directRightTorqueEffectiveness": "right_torque_eff_pct",
    "directVerticalSpeed": "vertical_speed_mps",
    "directGrade": "grade_pct",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_stop = False


def _handle_sigint(sig, frame):
    global _stop
    _stop = True
    print("\nInterrupt — will stop after current activity.")


signal.signal(signal.SIGINT, _handle_sigint)


def connect() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        log.error("GARMIN_EMAIL / GARMIN_PASSWORD not set in .env")
        sys.exit(1)
    api = Garmin(email, password)
    TOKEN_STORE.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 4):
        try:
            api.login(str(TOKEN_STORE))
            log.info("Garmin session ready from %s", TOKEN_STORE)
            return api
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = 30 * attempt
                log.warning("Rate limited (429). Waiting %ds …", wait)
                time.sleep(wait)
            else:
                log.error("Login failed: %s", e)
                sys.exit(1)


def is_cycling(activity: dict) -> bool:
    t = ((activity.get("activityType") or {}).get("typeKey") or "").lower()
    return ("cycl" in t) or ("biking" in t) or (t in {"virtual_ride", "indoor_cycling", "bmx"})


def fetch_cycling_list(api: Garmin) -> list[dict]:
    out = []
    start = 0
    log.info("Fetching activity list (filtering to cycling)…")
    while True:
        batch = api.get_activities(start=start, limit=PAGE_SIZE)
        if not batch:
            break
        cyc = [a for a in batch if is_cycling(a)]
        out.extend(cyc)
        log.info("  page start=%d: +%d cycling (total %d)", start, len(cyc), len(out))
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(DELAY_BETWEEN_PAGES)
    log.info("Total cycling activities found: %d", len(out))
    return out


def extract_streams(details: dict) -> pd.DataFrame | None:
    descs = details.get("metricDescriptors") or []
    samples = details.get("activityDetailMetrics") or []
    if not descs or not samples:
        return None
    col_idx: dict[str, int] = {}
    for d in descs:
        key = d.get("key")
        mapped = METRIC_MAP.get(key)
        if mapped is not None:
            col_idx[mapped] = d.get("metricsIndex")
    if "power_w" not in col_idx:
        return None
    rows = []
    for s in samples:
        arr = s.get("metrics") or []
        row = {}
        for col, idx in col_idx.items():
            if idx is not None and idx < len(arr):
                row[col] = arr[idx]
        rows.append(row)
    df = pd.DataFrame(rows)
    return df if not df.empty else None


def summarize(activity: dict, df: pd.DataFrame | None) -> dict:
    power = df["power_w"].dropna() if (df is not None and "power_w" in df) else pd.Series(dtype=float)
    return {
        "activity_id": activity["activityId"],
        "start_time_local": activity.get("startTimeLocal"),
        "activity_name": activity.get("activityName"),
        "activity_type": (activity.get("activityType") or {}).get("typeKey"),
        "duration_s": activity.get("duration"),
        "distance_m": activity.get("distance"),
        "elevation_gain_m": activity.get("elevationGain"),
        "avg_speed_mps": activity.get("averageSpeed"),
        "avg_hr_bpm": activity.get("averageHR"),
        "max_hr_bpm": activity.get("maxHR"),
        "avg_power_w": activity.get("avgPower"),
        "max_power_w": activity.get("maxPower"),
        "normalized_power_w": activity.get("normPower"),
        "training_stress_score": activity.get("trainingStressScore"),
        "intensity_factor": activity.get("intensityFactor"),
        "calories": activity.get("calories"),
        "calc_avg_power_w": float(power.mean()) if len(power) else None,
        "calc_max_power_w": float(power.max()) if len(power) else None,
        "samples": int(len(df)) if df is not None else 0,
        "samples_with_power": int(power.count()),
        "has_power_stream": bool(len(power) > 0),
    }


def flush_summary(existing: pd.DataFrame, new_rows: list[dict]) -> pd.DataFrame:
    if not new_rows:
        return existing
    addition = pd.DataFrame(new_rows)
    combined = pd.concat([existing, addition], ignore_index=True) if not existing.empty else addition
    combined.to_csv(SUMMARY_CSV, index=False)
    write_dashboard_js(combined)
    return combined


def write_dashboard_js(df: pd.DataFrame) -> None:
    import json
    cols = ["activity_id", "start_time_local", "activity_name", "activity_type", "duration_s", "distance_m", "elevation_gain_m", "avg_power_w", "calc_avg_power_w", "normalized_power_w", "avg_hr_bpm", "max_hr_bpm", "has_power_stream"]
    safe_df = df.copy()
    for c in cols:
        if c not in safe_df.columns:
            safe_df[c] = None
    rows = safe_df[cols].where(pd.notna(safe_df[cols]), None).to_dict(orient="records")
    DASHBOARD_JS.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_JS.write_text("window.CYCLING_ACTIVITIES = " + json.dumps(rows, default=str) + ";\n")


def main():
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)

    if SUMMARY_CSV.exists():
        existing = pd.read_csv(SUMMARY_CSV)
        done_ids = set(existing["activity_id"].astype("int64").tolist())
    else:
        existing = pd.DataFrame()
        done_ids = set()

    api = connect()
    cycling = fetch_cycling_list(api)
    to_process = [a for a in cycling if int(a["activityId"]) not in done_ids]
    log.info("%d cycling already in summary; %d to process.", len(done_ids), len(to_process))

    new_rows: list[dict] = []
    ok = no_power = fail = 0

    with tqdm(total=len(to_process), unit="act") as bar:
        for a in to_process:
            if _stop:
                break
            aid = int(a["activityId"])
            out_path = PROCESSED_DIR / f"{aid}.parquet"

            df = None
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    details = api.get_activity_details(aid)
                    df = extract_streams(details)
                    success = True
                    break
                except Exception as e:
                    log.warning("  [%d/%d] %s details failed: %s", attempt, MAX_RETRIES, aid, e)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
            if not success:
                fail += 1
                bar.update(1)
                bar.set_postfix(ok=ok, no_pwr=no_power, fail=fail)
                time.sleep(DELAY_BETWEEN_DL)
                continue

            row = summarize(a, df)
            if df is not None and not df.empty and "power_w" in df.columns:
                try:
                    df.to_parquet(out_path)
                    ok += 1
                except Exception as e:
                    log.warning("  Failed writing parquet for %s: %s", aid, e)
                    fail += 1
            else:
                no_power += 1

            new_rows.append(row)
            bar.update(1)
            bar.set_postfix(ok=ok, no_pwr=no_power, fail=fail)
            time.sleep(DELAY_BETWEEN_DL)

            if len(new_rows) >= 10:
                existing = flush_summary(existing, new_rows)
                new_rows = []

    existing = flush_summary(existing, new_rows)
    log.info("Done. With power: %d  No power: %d  Failed: %d", ok, no_power, fail)


if __name__ == "__main__":
    main()
