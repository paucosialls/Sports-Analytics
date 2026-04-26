"""Fetch comprehensive health data from Garmin Connect.

Pulls HRV, sleep (with stages + score), stress, body battery, SpO2,
respiration, training readiness, training status (VO2 max), race predictions
and body composition for each day in a sliding window. Persists to
`data/processed/health_daily.csv` (one row per date, many columns) and writes
`dashboard/health.js` for the dashboard to consume.

Idempotent: keeps `data/processed/health_fetched.json` so it only fetches new
or recent (last 3 days, in case sleep data lands late) dates per run.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError
from garth.exc import GarthHTTPError

logging.basicConfig(format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger("fetch_health_data")

load_dotenv(dotenv_path=".env")

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
RAW_DIR = PROCESSED / "health_raw"
CSV_PATH = PROCESSED / "health_daily.csv"
JS_PATH = ROOT / "dashboard" / "health.js"
FETCHED_PATH = PROCESSED / "health_fetched.json"
TOKEN_STORE = Path.home() / ".garth"

# How many days back to cover on a fresh run (no cache yet).
DEFAULT_BACKFILL_DAYS = int(os.getenv("HEALTH_BACKFILL_DAYS", "365"))
# Always re-fetch the most recent N days each run — sleep data, stress,
# training readiness can land hours after midnight, so the first fetch may
# miss them.
RECHECK_RECENT_DAYS = 3
# Pause between days to avoid Garmin's rate limiter.
SLEEP_BETWEEN_DAYS_S = 0.6


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
    sys.exit(1)


def safe(fn, *args, **kwargs):
    """Run a Garmin API call; swallow non-fatal errors and return None."""
    try:
        return fn(*args, **kwargs)
    except GarthHTTPError as e:
        if "404" in str(e) or "204" in str(e):
            return None
        log.warning("API error %s: %s", fn.__name__, str(e)[:120])
        return None
    except Exception as e:
        log.warning("API error %s: %s", fn.__name__, str(e)[:120])
        return None


def _row_for_date(api: Garmin, d: str, raw_dump: dict[str, Any]) -> dict[str, Any]:
    """Fetch all health endpoints for a date and flatten to a single row."""
    row: dict[str, Any] = {"date": d}

    # HRV — overnight summary.
    hrv = safe(api.get_hrv_data, d)
    raw_dump["hrv"] = hrv
    if hrv:
        s = hrv.get("hrvSummary") or {}
        row["hrv_last_night_avg"] = s.get("lastNightAvg")
        row["hrv_weekly_avg"] = s.get("weeklyAvg")
        row["hrv_last_5min_high"] = s.get("lastNight5MinHigh")
        row["hrv_status"] = s.get("status")
        bl = s.get("baseline") or {}
        row["hrv_baseline_low"] = bl.get("lowUpper")
        row["hrv_baseline_balanced_low"] = bl.get("balancedLow")
        row["hrv_baseline_balanced_upper"] = bl.get("balancedUpper")

    # Sleep — duration, stages, score, respiration during sleep.
    sleep = safe(api.get_sleep_data, d)
    raw_dump["sleep"] = sleep
    dto = (sleep or {}).get("dailySleepDTO") or {}
    if dto:
        row["sleep_total_s"] = dto.get("sleepTimeSeconds")
        row["sleep_nap_s"] = dto.get("napTimeSeconds")
        row["sleep_deep_s"] = dto.get("deepSleepSeconds")
        row["sleep_light_s"] = dto.get("lightSleepSeconds")
        row["sleep_rem_s"] = dto.get("remSleepSeconds")
        row["sleep_awake_s"] = dto.get("awakeSleepSeconds")
        row["sleep_awake_count"] = dto.get("awakeCount")
        row["sleep_avg_stress"] = dto.get("avgSleepStress")
        row["sleep_avg_respiration"] = dto.get("averageRespirationValue")
        row["sleep_lowest_respiration"] = dto.get("lowestRespirationValue")
        row["sleep_highest_respiration"] = dto.get("highestRespirationValue")
        row["sleep_avg_spo2"] = dto.get("averageSpO2Value")
        row["sleep_lowest_spo2"] = dto.get("lowestSpO2Value")
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        row["sleep_score"] = overall.get("value")
        row["sleep_score_qualifier"] = overall.get("qualifierKey")
        row["sleep_feedback"] = dto.get("sleepScoreFeedback")
        row["sleep_start_local_ms"] = dto.get("sleepStartTimestampLocal")
        row["sleep_end_local_ms"] = dto.get("sleepEndTimestampLocal")

    # Stress (avg/max already in daily_stats but include for completeness).
    stress = safe(api.get_stress_data, d)
    raw_dump["stress"] = stress
    if stress:
        row["stress_avg"] = stress.get("avgStressLevel")
        row["stress_max"] = stress.get("maxStressLevel")
        row["stress_rest_s"] = stress.get("restStressDuration")
        row["stress_low_s"] = stress.get("lowStressDuration")
        row["stress_med_s"] = stress.get("mediumStressDuration")
        row["stress_high_s"] = stress.get("highStressDuration")

    # Body battery (charged / drained per day).
    bb = safe(api.get_body_battery, d, d)
    raw_dump["body_battery"] = bb
    if bb and isinstance(bb, list) and bb:
        item = bb[0]
        row["bb_charged"] = item.get("charged")
        row["bb_drained"] = item.get("drained")
        arr = item.get("bodyBatteryValuesArray") or []
        vals = [v[1] for v in arr if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] is not None]
        if vals:
            row["bb_min"] = min(vals)
            row["bb_max"] = max(vals)

    # SpO2 (separate endpoint — may be richer than the in-sleep summary).
    spo2 = safe(api.get_spo2_data, d)
    raw_dump["spo2"] = spo2
    if spo2:
        row["spo2_avg"] = spo2.get("averageSpO2") or spo2.get("averageSpO2HR")
        row["spo2_lowest"] = spo2.get("lowestSpO2")
        row["spo2_latest"] = spo2.get("latestSpO2")

    # Respiration (full day — waking + sleeping aggregate).
    resp = safe(api.get_respiration_data, d)
    raw_dump["respiration"] = resp
    if resp:
        row["resp_avg_waking"] = resp.get("avgWakingRespirationValue")
        row["resp_avg_sleep"] = resp.get("avgSleepRespirationValue")
        row["resp_lowest"] = resp.get("lowestRespirationValue")
        row["resp_highest"] = resp.get("highestRespirationValue")

    # Training readiness — most recent reading for the day.
    tr = safe(api.get_training_readiness, d)
    raw_dump["training_readiness"] = tr
    if tr and isinstance(tr, list) and tr:
        latest = tr[0]
        row["tr_score"] = latest.get("score")
        row["tr_level"] = latest.get("level")
        row["tr_feedback"] = latest.get("feedbackLong")
        row["tr_sleep_score"] = latest.get("sleepScore")
        row["tr_recovery_time_min"] = latest.get("recoveryTime")
        row["tr_hrv_weekly_avg"] = latest.get("hrvWeeklyAverage")
        row["tr_hrv_status"] = latest.get("hrvStatus")
        row["tr_acute_load"] = latest.get("acuteLoad")
        row["tr_inputs_present"] = ",".join(latest.get("inputContext", {}).keys()) if isinstance(latest.get("inputContext"), dict) else None

    # Training status — VO2 max + ACWR. Only fetch for the most recent day,
    # since the endpoint reports current state, not historical.
    if d == raw_dump.get("_today"):
        ts = safe(api.get_training_status, d)
        raw_dump["training_status"] = ts
        if ts:
            mr = ts.get("mostRecentVO2Max") or {}
            generic = mr.get("generic") or {}
            cycling = mr.get("cycling") or {}
            row["vo2max_generic"] = generic.get("vo2MaxPreciseValue")
            row["vo2max_cycling"] = cycling.get("vo2MaxPreciseValue")
            tlb = (ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
            for v in tlb.values():
                row["load_aerobic_low"] = v.get("monthlyLoadAerobicLow")
                row["load_aerobic_high"] = v.get("monthlyLoadAerobicHigh")
                row["load_anaerobic"] = v.get("monthlyLoadAnaerobic")
                row["load_balance_feedback"] = v.get("trainingBalanceFeedbackPhrase")
                break
            tsd = (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
            for v in tsd.values():
                row["training_status_code"] = v.get("trainingStatus")
                row["training_status_feedback"] = v.get("trainingStatusFeedbackPhrase")
                row["fitness_trend"] = v.get("fitnessTrend")
                acwr = v.get("acuteTrainingLoadDTO") or {}
                row["acwr_pct"] = acwr.get("acwrPercent")
                row["acwr_status"] = acwr.get("acwrStatus")
                row["acute_load"] = acwr.get("dailyTrainingLoadAcute")
                row["chronic_load"] = acwr.get("dailyTrainingLoadChronic")
                row["acwr_ratio"] = acwr.get("dailyAcuteChronicWorkloadRatio")
                break

        rp = safe(api.get_race_predictions)
        raw_dump["race_predictions"] = rp
        if rp:
            row["race_5k_s"] = rp.get("time5K")
            row["race_10k_s"] = rp.get("time10K")
            row["race_half_s"] = rp.get("timeHalfMarathon")
            row["race_marathon_s"] = rp.get("timeMarathon")

    # Body composition / weight (only populated on weigh-in days).
    bc = safe(api.get_body_composition, d)
    raw_dump["body_composition"] = bc
    if bc:
        weights = bc.get("dateWeightList") or []
        if weights:
            w = weights[-1]
            row["weight_g"] = w.get("weight")
            row["body_fat_pct"] = w.get("bodyFat")
            row["body_water_pct"] = w.get("bodyWater")
            row["bone_mass_g"] = w.get("boneMass")
            row["muscle_mass_g"] = w.get("muscleMass")
            row["bmi"] = w.get("bmi")
            row["visceral_fat"] = w.get("visceralFat")
            row["metabolic_age"] = w.get("metabolicAge")

    return row


def _row_has_data(row: dict[str, Any]) -> bool:
    """True if the row has any field beyond `date`."""
    return any(v is not None for k, v in row.items() if k != "date")


def main() -> int:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing CSV state.
    existing: dict[str, dict[str, Any]] = {}
    columns_seen: list[str] = ["date"]
    if CSV_PATH.exists():
        import csv
        with open(CSV_PATH, newline="") as f:
            r = csv.DictReader(f)
            for k in r.fieldnames or []:
                if k not in columns_seen:
                    columns_seen.append(k)
            for row in r:
                existing[row["date"]] = {k: (None if v == "" else v) for k, v in row.items()}

    fetched: set[str] = set()
    if FETCHED_PATH.exists():
        try:
            fetched = set(json.loads(FETCHED_PATH.read_text()))
        except Exception:
            fetched = set()

    today = date.today()
    if not fetched:
        start = today - timedelta(days=DEFAULT_BACKFILL_DAYS)
    else:
        start = today - timedelta(days=RECHECK_RECENT_DAYS)
    dates = []
    d = start
    while d <= today:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    # Add any older missing dates if the cache is out of date (e.g. a longer
    # absence than RECHECK_RECENT_DAYS) — defend against silent gaps.
    for back in range(RECHECK_RECENT_DAYS + 1, 30):
        d2 = (today - timedelta(days=back)).isoformat()
        if d2 not in fetched and d2 not in dates:
            dates.append(d2)
    dates.sort()

    log.info("Fetching health data for %d dates (%s → %s)",
             len(dates), dates[0], dates[-1])

    api = connect()
    today_str = today.isoformat()
    new_rows = 0
    for i, d in enumerate(dates, 1):
        raw_dump: dict[str, Any] = {"_today": today_str}
        try:
            row = _row_for_date(api, d, raw_dump)
        except Exception as e:
            log.warning("[%s] failed: %s", d, e)
            continue

        if _row_has_data(row):
            existing[d] = {**existing.get(d, {}), **{k: v for k, v in row.items() if v is not None}}
            for k in row.keys():
                if k not in columns_seen:
                    columns_seen.append(k)
            new_rows += 1
            # Keep raw JSON for one out of every 30 days as an archival
            # sample (full raw is 100 KB+/day — too big to keep all).
            if i % 30 == 0 or d == today_str:
                (RAW_DIR / f"{d}.json").write_text(
                    json.dumps(raw_dump, default=str, indent=2))

        fetched.add(d)
        if i % 20 == 0:
            log.info("  ... %d/%d processed", i, len(dates))
        time.sleep(SLEEP_BETWEEN_DAYS_S)

    # Write CSV.
    import csv
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns_seen)
        w.writeheader()
        for d in sorted(existing.keys()):
            w.writerow(existing[d])
    log.info("Wrote %s (%d rows, %d cols)", CSV_PATH, len(existing), len(columns_seen))

    # Write JS for the dashboard. Keep numbers as numbers where possible.
    def coerce(v):
        if v is None or v == "":
            return None
        try:
            f = float(v)
            return int(f) if f.is_integer() and abs(f) < 1e15 else f
        except (ValueError, TypeError):
            return v
    rows_out = []
    for d in sorted(existing.keys()):
        row = existing[d]
        rows_out.append({k: coerce(v) for k, v in row.items()})
    JS_PATH.parent.mkdir(parents=True, exist_ok=True)
    JS_PATH.write_text(
        "window.HEALTH_DAILY = " + json.dumps(rows_out, ensure_ascii=False) + ";\n"
    )
    log.info("Wrote %s (%d rows)", JS_PATH, len(rows_out))

    FETCHED_PATH.write_text(json.dumps(sorted(fetched)))
    log.info("Done. %d dates processed this run, %d total in cache.",
             new_rows, len(fetched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
