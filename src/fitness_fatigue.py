"""
fitness_fatigue.py — Performance Management Chart (CTL/ATL/TSB).

Reads:
  data/processed/activities.csv        (all activities, any sport)
  data/processed/cycling_activities.csv (cycling with power → direct TSS)
  data/processed/daily_stats.csv       (resting HR observations)
  user_profile.json                    (sex, birthdate)

Per activity:
  - If cycling with power: use precomputed training_stress_score.
  - Else if HR available: compute hrTSS via Banister TRIMP normalized to
    100 per hour at LTHR.

Writes:
  data/processed/daily_load.csv        (date, tss, ctl, atl, tsb)
  dashboard/fitness.js                 (daily series + activity markers)
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PROC = ROOT / "data" / "processed"
PROFILE_PATH = ROOT / "user_profile.json"
BAD_ACTIVITIES_PATH = ROOT / "data" / "bad_activities.json"

CTL_TAU = 42
ATL_TAU = 7
LTHR_FRACTION_OF_HRMAX = 0.88
DEFAULT_HRMAX_FLOOR = 170  # if data-derived HRmax is implausibly low

ACTIVITY_ID_RE = re.compile(r"_(\d{8,})_")


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return json.load(f)


def load_bad_activity_ids() -> set[int]:
    if not BAD_ACTIVITIES_PATH.exists():
        return set()
    data = json.loads(BAD_ACTIVITIES_PATH.read_text())
    return {int(e["activity_id"]) for e in data.get("skip", [])
            if e.get("activity_id") is not None}


def parse_activity_id_from_filename(fname: str) -> int | None:
    m = ACTIVITY_ID_RE.search(fname)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def derive_hr_thresholds(activities: pd.DataFrame, daily_stats: pd.DataFrame,
                        profile: dict) -> dict:
    """Derive HRmax, HRrest, LTHR from observed data with sane fallbacks."""
    max_hr_series = activities["max_hr"].dropna()
    max_hr_series = max_hr_series[(max_hr_series >= 120) & (max_hr_series <= 220)]
    if len(max_hr_series) >= 10:
        hr_max = float(max_hr_series.quantile(0.98))
    else:
        age = None
        if profile.get("birthdate"):
            bd = datetime.fromisoformat(profile["birthdate"]).date()
            age = (date.today() - bd).days // 365
        hr_max = float(220 - age) if age else 185.0
    hr_max = max(hr_max, DEFAULT_HRMAX_FLOOR)

    rest_series = daily_stats["restingHeartRate"].dropna()
    rest_series = rest_series[(rest_series >= 35) & (rest_series <= 90)]
    hr_rest = float(rest_series.median()) if len(rest_series) else 55.0

    lthr = hr_max * LTHR_FRACTION_OF_HRMAX
    return {"hr_max": hr_max, "hr_rest": hr_rest, "lthr": lthr,
            "sex": profile.get("sex", "male")}


def trimp_per_hour_at_lthr(thr: dict) -> float:
    k = 1.92 if str(thr["sex"]).lower().startswith("m") else 1.67
    hr_ratio = (thr["lthr"] - thr["hr_rest"]) / (thr["hr_max"] - thr["hr_rest"])
    return 60.0 * hr_ratio * 0.64 * math.exp(k * hr_ratio)


def hr_tss_from_avg(avg_hr: float, duration_min: float, thr: dict,
                    ref_trimp_hr: float) -> float:
    if pd.isna(avg_hr) or pd.isna(duration_min) or duration_min <= 0:
        return 0.0
    k = 1.92 if str(thr["sex"]).lower().startswith("m") else 1.67
    hr_ratio = (avg_hr - thr["hr_rest"]) / (thr["hr_max"] - thr["hr_rest"])
    hr_ratio = max(0.0, min(1.2, hr_ratio))
    trimp = duration_min * hr_ratio * 0.64 * math.exp(k * hr_ratio)
    if ref_trimp_hr <= 0:
        return 0.0
    return trimp / ref_trimp_hr * 100.0


def build_per_activity_load(activities: pd.DataFrame,
                           cycling: pd.DataFrame,
                           thr: dict,
                           bad_ids: set[int]) -> pd.DataFrame:
    ref = trimp_per_hour_at_lthr(thr)
    cyc_tss = cycling.set_index("activity_id")["training_stress_score"].to_dict()
    cyc_has_power = cycling.set_index("activity_id")["has_power_stream"].to_dict()

    activities = activities.copy()
    activities["activity_id"] = activities["file"].apply(
        parse_activity_id_from_filename)

    loads = []
    methods = []
    skipped = 0
    for _, row in activities.iterrows():
        aid = row["activity_id"]
        if aid is not None and aid in bad_ids:
            loads.append(0.0)
            methods.append("skip")
            skipped += 1
            continue
        tss_power = None
        if aid is not None and aid in cyc_tss:
            has_power = cyc_has_power.get(aid, False)
            val = cyc_tss.get(aid)
            if has_power and pd.notna(val) and val > 0:
                tss_power = float(val)
        if tss_power is not None:
            loads.append(tss_power)
            methods.append("power")
        else:
            hr_score = hr_tss_from_avg(row["avg_hr"], row["duration_min"],
                                       thr, ref)
            loads.append(hr_score)
            methods.append("hr" if hr_score > 0 else "none")
    activities["load"] = loads
    activities["method"] = methods
    if skipped:
        print(f"Skipped {skipped} bad activity record(s) via denylist")
    return activities


def compute_pmc(daily: pd.Series) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(columns=["tss", "ctl", "atl", "tsb"])
    idx = pd.date_range(daily.index.min(), pd.Timestamp(date.today()), freq="D")
    tss = daily.reindex(idx, fill_value=0.0)
    ctl = np.zeros(len(tss))
    atl = np.zeros(len(tss))
    c = a = 0.0
    values = tss.values
    for i, v in enumerate(values):
        c += (v - c) / CTL_TAU
        a += (v - a) / ATL_TAU
        ctl[i] = c
        atl[i] = a
    df = pd.DataFrame({"tss": values, "ctl": ctl, "atl": atl}, index=idx)
    df["tsb"] = df["ctl"].shift(1).fillna(0) - df["atl"].shift(1).fillna(0)
    return df


def write_dashboard_js(pmc: pd.DataFrame, acts: pd.DataFrame,
                       thr: dict, out_path: Path) -> None:
    # Compact daily records
    daily = [{
        "date": d.strftime("%Y-%m-%d"),
        "tss": round(float(r.tss), 1),
        "ctl": round(float(r.ctl), 2),
        "atl": round(float(r.atl), 2),
        "tsb": round(float(r.tsb), 2),
    } for d, r in pmc.iterrows()]

    act_records = []
    scored = acts[acts["load"] > 0][
        ["date", "name", "type", "duration_min", "load", "method",
         "activity_id"]].copy()
    scored.sort_values("date", inplace=True)
    for _, r in scored.iterrows():
        act_records.append({
            "date": str(r["date"]),
            "name": r["name"],
            "type": r["type"],
            "duration_min": float(r["duration_min"]),
            "load": round(float(r["load"]), 1),
            "method": r["method"],
            "activity_id": int(r["activity_id"]) if pd.notna(
                r["activity_id"]) else None,
        })

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {k: round(v, 1) if isinstance(v, float) else v
                        for k, v in thr.items()},
        "ctl_tau_days": CTL_TAU,
        "atl_tau_days": ATL_TAU,
        "daily": daily,
        "activities": act_records,
    }
    out_path.write_text(
        "window.fitnessData = " + json.dumps(payload, default=str) + ";\n")


def main() -> int:
    profile = load_profile()
    activities = pd.read_csv(PROC / "activities.csv", parse_dates=["date"])
    cycling = pd.read_csv(PROC / "cycling_activities.csv",
                          parse_dates=["start_time_local"])
    daily_stats = pd.read_csv(PROC / "daily_stats.csv",
                              parse_dates=["calendarDate"])

    thr = derive_hr_thresholds(activities, daily_stats, profile)
    print(f"HR thresholds: max={thr['hr_max']:.0f}  rest={thr['hr_rest']:.0f}  "
          f"LTHR={thr['lthr']:.0f}  sex={thr['sex']}")

    bad_ids = load_bad_activity_ids()
    scored = build_per_activity_load(activities, cycling, thr, bad_ids)
    scored = scored.dropna(subset=["date"])

    by_method = scored["method"].value_counts().to_dict()
    print(f"Activities scored: {by_method}")

    daily = scored.groupby(scored["date"].dt.date)["load"].sum()
    daily.index = pd.to_datetime(daily.index)
    pmc = compute_pmc(daily)

    out_csv = PROC / "daily_load.csv"
    pmc.to_csv(out_csv, index_label="date")

    out_js = ROOT / "dashboard" / "fitness.js"
    write_dashboard_js(pmc, scored, thr, out_js)

    last = pmc.iloc[-1]
    print(f"Today  CTL={last.ctl:.1f}  ATL={last.atl:.1f}  TSB={last.tsb:+.1f}")
    print(f"Wrote {out_csv}  and  {out_js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
