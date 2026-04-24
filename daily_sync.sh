#!/bin/bash
# Daily sync: pull new Garmin activities, rebuild dashboard artifacts.
# Called by ~/Library/LaunchAgents/com.pau.sports-analytics.daily.plist

set -u
cd "$(dirname "$0")"

LOG_DIR=logs
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily.log"

echo "" >> "$LOG"
echo "======== $(date '+%Y-%m-%d %H:%M:%S %Z') ========" >> "$LOG"

source venv/bin/activate

run_step() {
  local name=$1
  shift
  echo "--- $name ---" >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then
    echo "[ok]  $name" >> "$LOG"
  else
    echo "[err] $name (exit $?)" >> "$LOG"
  fi
}

run_step download_garmin     python src/download_garmin.py
run_step fetch_cycling_power python src/fetch_cycling_power.py
run_step fetch_daily_stats   python src/fetch_daily_stats.py
run_step parse_gpx           python src/parse_gpx.py
run_step backfill_cadence    python src/backfill_cadence.py
run_step fitness_fatigue     python src/fitness_fatigue.py

echo "done $(date '+%H:%M:%S')" >> "$LOG"
