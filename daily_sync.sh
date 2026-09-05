#!/bin/bash
# Daily sync: pull new Garmin activities, rebuild dashboard artifacts,
# commit fresh data files and push to GitHub.
# Scheduled via crontab on auntie.

set -u
cd "$(dirname "$0")"

LOG_DIR=logs
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily.log"

echo "" >> "$LOG"
echo "======== $(date '+%Y-%m-%d %H:%M:%S %Z') ========" >> "$LOG"

source venv/bin/activate

SYNC_FAILED=0

run_step() {
  local name=$1
  shift
  echo "--- $name ---" >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then
    echo "[ok]  $name" >> "$LOG"
  else
    echo "[err] $name (exit $?)" >> "$LOG"
    SYNC_FAILED=1
  fi
}

run_step download_garmin     python src/download_garmin.py
run_step fetch_cycling_power python src/fetch_cycling_power.py
run_step fetch_daily_stats   python src/fetch_daily_stats.py
run_step fetch_health_data   python src/fetch_health_data.py
run_step parse_gpx           python src/parse_gpx.py
run_step backfill_cadence    python src/backfill_cadence.py
run_step fitness_fatigue     python src/fitness_fatigue.py

# Do not publish a partially refreshed dashboard. Previously this script ended
# successfully even when every Garmin step failed, so the UI claimed that the
# refresh worked and reloaded the same stale data.
if [ "$SYNC_FAILED" -ne 0 ]; then
  echo "sync incomplete — skipping git push" >> "$LOG"
  echo "failed $(date '+%H:%M:%S')" >> "$LOG"
  exit 1
fi

# Build both daily cycling choices from the refreshed readiness data and
# publish them as stable, upserted Intervals.icu events.
run_step generate_workouts python src/generate_today_workout.py
run_step upload_workouts   python src/upload_today_workout_intervals.py

if [ "$SYNC_FAILED" -ne 0 ]; then
  echo "workout publication incomplete" >> "$LOG"
  echo "failed $(date '+%H:%M:%S')" >> "$LOG"
  exit 1
fi

# --- git: commit fresh dashboard data and push to GitHub ---
echo "--- git_push ---" >> "$LOG"
{
  git add dashboard/activities.js dashboard/cadence_activities.js \
          dashboard/cycling_activities.js dashboard/daily_stats.js \
          dashboard/fitness.js dashboard/health.js 2>/dev/null
  if ! git diff --cached --quiet; then
    git commit -m "Daily data sync $(date '+%Y-%m-%d')" \
      && env -u GH_TOKEN -u GITHUB_TOKEN git push
  else
    echo "no data changes — skipping commit"
  fi
} >> "$LOG" 2>&1 && echo "[ok]  git_push" >> "$LOG" \
                || echo "[err] git_push" >> "$LOG"

echo "done $(date '+%H:%M:%S')" >> "$LOG"
