#!/usr/bin/env python3
"""Generate today's indoor and outdoor workouts from Garmin-derived readiness data.

Writes:
  out/today_workout.json
  out/today_workout.zwo
  out/today_outdoor_workout.json
  out/today_outdoor_workout.zwo
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from statistics import mean
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
PROC = ROOT / "data" / "processed"
OUT = ROOT / "out"


def num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def mean_present(values):
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def latest_power_ftp() -> int:
    rows = read_csv(PROC / "cycling_activities.csv")
    estimates = []
    for row in rows[-40:]:
        np_w = num(row.get("normalized_power_w"))
        if_ = num(row.get("intensity_factor"))
        if np_w and if_ and 0.45 <= if_ <= 1.05:
            estimates.append(np_w / if_)
    if not estimates:
        return 300
    return round(mean(estimates[-8:]))


def workout_step(minutes, label, power, note=""):
    return {"minutes": minutes, "label": label, "power": power, "note": note}


def choose_workouts():
    health = read_csv(PROC / "health_daily.csv")
    load = read_csv(PROC / "daily_load.csv")
    if not health:
        raise SystemExit("No health data found. Run src/fetch_health_data.py first.")
    today = health[-1]
    last30 = health[-31:-1]
    load_by_date = {r["date"]: r for r in load}
    fit = load[-1] if load else {}

    tr = num(today.get("tr_score"))
    sleep_s = num(today.get("sleep_total_s"))
    sleep_h = sleep_s / 3600 if sleep_s else None
    hrv = num(today.get("hrv_last_night_avg"))
    base_hrv = mean_present(num(r.get("hrv_last_night_avg")) for r in last30)
    tsb = num(fit.get("tsb"))
    today_tss = num(load_by_date.get(today.get("date"), {}).get("tss")) or 0
    ftp = latest_power_ftp()

    hrv_low = hrv is not None and base_hrv is not None and hrv < base_hrv - 5
    poor_sleep = sleep_h is not None and sleep_h < 6.5
    heavy_today = today_tss >= 90
    high_readiness = tr is not None and tr >= 75
    tired = tr is not None and tr < 50

    if heavy_today or tired or poor_sleep:
        name = "60 min recovery spin"
        badge = "easy"
        target_tss = 30
        goal = "Absorb load and add easy calorie burn without adding fatigue."
        steps = [
            workout_step(10, "Warm-up", [0.45, 0.60]),
            workout_step(35, "Endurance Z2 low", 0.60),
            workout_step(5, "High cadence drills", 0.65, "5 x 30 s quick legs, easy between"),
            workout_step(10, "Cool-down", [0.55, 0.40]),
        ]
    elif high_readiness and not hrv_low and (tsb is None or tsb > -10):
        name = "60 min over-under builder"
        badge = "hard"
        target_tss = 70
        goal = "Improve threshold fitness with controlled work near FTP."
        steps = [
            workout_step(12, "Progressive warm-up", [0.50, 0.75]),
            workout_step(6, "Activation", 1.05, "3 x 1 min hard, 1 min easy"),
            workout_step(24, "Over-under set", 0.92, "3 x 8 min: 2 min 88%, 1 min 102%, repeat"),
            workout_step(8, "Z2 reset", 0.62),
            workout_step(10, "Cool-down", [0.55, 0.40]),
        ]
    else:
        name = "60 min sweet spot aerobic"
        badge = "build"
        target_tss = 55
        goal = "Build fitness and support weight loss with work you can repeat 2-3 times per week."
        steps = [
            workout_step(12, "Warm-up", [0.50, 0.75]),
            workout_step(15, "Sweet spot block 1", 0.88),
            workout_step(5, "Recovery", 0.55),
            workout_step(15, "Sweet spot block 2", 0.88),
            workout_step(13, "Cool-down endurance", [0.65, 0.45]),
        ]

    reasons = []
    if tr is not None:
        reasons.append(f"readiness {tr:.0f}")
    if sleep_h is not None:
        reasons.append(f"sleep {sleep_h:.1f} h")
    if hrv is not None and base_hrv is not None:
        reasons.append(f"HRV {hrv:.0f} vs {base_hrv:.0f} ms baseline")
    if tsb is not None:
        reasons.append(f"TSB {tsb:+.0f}")
    if today_tss > 0:
        reasons.append(f"today already has {today_tss:.0f} TSS")

    indoor = {
        "date": today.get("date") or date.today().isoformat(),
        "mode": "indoor",
        "name": name,
        "badge": badge,
        "target_tss": target_tss,
        "goal": goal,
        "ftp_w": ftp,
        "reasons": reasons,
        "steps": steps,
    }

    if badge == "easy":
        outdoor = {
            "name": "90 min easy endurance ride",
            "target_tss": 45,
            "goal": "Recover outdoors while keeping the effort smooth and controlled.",
            "steps": [
                workout_step(15, "Easy warm-up", [0.45, 0.58]),
                workout_step(65, "Low endurance", 0.58, "Flat or gently rolling route; avoid hard climbs"),
                workout_step(10, "Easy ride home", [0.55, 0.40]),
            ],
        }
    elif badge == "hard":
        outdoor = {
            "name": "90 min outdoor threshold builder",
            "target_tss": 90,
            "goal": "Use the good readiness day for sustained climbing or headwind efforts.",
            "steps": [
                workout_step(20, "Progressive warm-up", [0.45, 0.72]),
                workout_step(15, "Tempo", 0.80),
                workout_step(10, "Threshold effort 1", 0.95, "Steady climb or uninterrupted road"),
                workout_step(10, "Easy endurance", 0.60),
                workout_step(15, "Tempo", 0.82),
                workout_step(10, "Threshold effort 2", 0.95, "Controlled, not all-out"),
                workout_step(10, "Cool-down", [0.55, 0.40]),
            ],
        }
    else:
        outdoor = {
            "name": "90 min aerobic endurance ride",
            "target_tss": 65,
            "goal": "Build aerobic fitness outdoors without creating excessive fatigue.",
            "steps": [
                workout_step(15, "Progressive warm-up", [0.45, 0.65]),
                workout_step(60, "Steady endurance", 0.68, "Keep pressure on the pedals; avoid surges"),
                workout_step(5, "Cadence work", 0.75, "5 x 30 s quick legs, easy between"),
                workout_step(10, "Easy ride home", [0.55, 0.40]),
            ],
        }

    outdoor.update({
        "date": indoor["date"],
        "mode": "outdoor",
        "badge": badge,
        "ftp_w": ftp,
        "reasons": reasons,
    })
    return indoor, outdoor


def write_zwo(workout: dict, path: Path) -> None:
    lines = [
        "<workout_file>",
        "  <author>Sports Analytics</author>",
        f"  <name>{escape(workout['name'])}</name>",
        f"  <description>{escape(workout['goal'] + ' ' + '; '.join(workout['reasons']))}</description>",
        "  <sportType>bike</sportType>",
        "  <workout>",
    ]
    for step in workout["steps"]:
        seconds = int(step["minutes"] * 60)
        power = step["power"]
        label = escape(step["label"], {"'": "&apos;", '"': "&quot;"})
        if isinstance(power, list):
            lines.append(
                f'    <Ramp Duration="{seconds}" PowerLow="{power[0]:.2f}" '
                f'PowerHigh="{power[1]:.2f}" pace="0"/>'
            )
        else:
            lines.append(f'    <SteadyState Duration="{seconds}" Power="{power:.2f}" pace="0">')
            lines.append(f'      <textevent timeoffset="0" message="{label}"/>')
            lines.append("    </SteadyState>")
    lines += ["  </workout>", "</workout_file>", ""]
    path.write_text("\n".join(lines))

def main() -> int:
    OUT.mkdir(exist_ok=True)
    indoor, outdoor = choose_workouts()
    for workout, stem in ((indoor, "today_workout"), (outdoor, "today_outdoor_workout")):
        (OUT / f"{stem}.json").write_text(json.dumps(workout, indent=2) + "\n")
        write_zwo(workout, OUT / f"{stem}.zwo")
        print(f"{workout['date']}: {workout['name']} ({workout['target_tss']} TSS, FTP {workout['ftp_w']} W)")
    print("; ".join(indoor["reasons"]))
    print("Wrote indoor and outdoor workout files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
