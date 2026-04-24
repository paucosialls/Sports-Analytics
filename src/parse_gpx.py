"""
parse_gpx.py — Batch GPX parser for Garmin activities.
Reads all .gpx files from data/raw/ and extracts per-activity summary metrics.
Outputs:
  - data/processed/activities.csv   (for pandas/notebook use)
  - data/processed/activities.js    (for dashboard/index.html — no server needed)
"""

from __future__ import annotations
import os, math, json, bisect
import gpxpy
import pandas as pd
from tqdm import tqdm
from datetime import timezone
from pathlib import Path
from xml.etree import ElementTree as ET

RAW_DIR    = Path(__file__).parent.parent / "data" / "raw"
OUT_CSV    = Path(__file__).parent.parent / "data" / "processed" / "activities.csv"
OUT_JS     = Path(__file__).parent.parent / "dashboard" / "activities.js"
SEEN_FILE  = Path(__file__).parent.parent / "data" / "processed" / "parsed_files.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6_371_000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


NS = {
    'gpxtpx': 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1',
    'ns3':    'http://www.garmin.com/xmlschemas/TrackPointExtension/v1',
}

def get_hr(point) -> float | None:
    """Extract heart rate from a GPX track point's extensions."""
    for ext in point.extensions:
        # garmin TrackPointExtension
        hr = ext.find('.//{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}hr')
        if hr is not None and hr.text:
            return float(hr.text)
        # some files put it directly
        if ext.tag.endswith('}hr') or ext.tag == 'hr':
            try:
                return float(ext.text)
            except (TypeError, ValueError):
                pass
    return None


def get_cadence(point) -> float | None:
    for ext in point.extensions:
        cad = ext.find('.//{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}cad')
        if cad is not None and cad.text:
            return float(cad.text)
        if ext.tag.endswith('}cad') or ext.tag == 'cad':
            try:
                return float(ext.text)
            except (TypeError, ValueError):
                pass
    return None


def get_power(point) -> float | None:
    """Extract power (watts) from a GPX track point's extensions (Garmin cycling/running power)."""
    for ext in point.extensions:
        # Garmin CyclingDynamics / RunningDynamics power field
        pwr = ext.find('.//{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}PowerInWatts')
        if pwr is not None and pwr.text:
            return float(pwr.text)
        # Some devices use a generic <power> tag
        if ext.tag.endswith('}power') or ext.tag == 'power':
            try:
                return float(ext.text)
            except (TypeError, ValueError):
                pass
        # Wahoo / other devices use <Extensions><power>
        pwr2 = ext.find('.//power')
        if pwr2 is not None and pwr2.text:
            try:
                return float(pwr2.text)
            except (TypeError, ValueError):
                pass
    return None


def compute_power_profile(points: list) -> dict:
    """
    Compute power profile metrics for activities with power meter data.

    Returns a dict with:
      normalized_power  — 30 s rolling-avg NP (W)
      max_5s_power      — best 5-second average (W)
      max_1min_power    — best 1-minute average (W)
      max_5min_power    — best 5-minute average (W)
      max_20min_power   — best 20-minute average (W)
    """
    # Collect (epoch_s, watts) only for points that have both timestamp and power
    timed: list[tuple[float, float]] = []
    for pt in points:
        if pt.time is None:
            continue
        w = get_power(pt)
        if w is None or w <= 0:
            continue
        timed.append((pt.time.timestamp(), w))

    if len(timed) < 30:
        return {}

    timed.sort(key=lambda x: x[0])
    times  = [t for t, _ in timed]
    powers = [w for _, w in timed]
    n = len(timed)

    # Prefix sums for O(1) range-average queries
    prefix = [0.0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + powers[i]

    def range_avg(l: int, r: int) -> float:
        return (prefix[r + 1] - prefix[l]) / (r - l + 1)

    # ── Normalized Power (30 s rolling average → 4th-power mean → 4th root) ──
    np_vals: list[float] = []
    for i in range(n):
        left = bisect.bisect_left(times, times[i] - 30)
        np_vals.append(range_avg(left, i))
    normalized_power = round((sum(v ** 4 for v in np_vals) / len(np_vals)) ** 0.25) if np_vals else None

    # ── Peak average power for a sliding time window ─────────────────────────
    def peak_power(duration_s: float) -> int | None:
        span = times[-1] - times[0]
        if span < duration_s * 0.5:
            return None
        best = 0.0
        right = 0
        for left in range(n):
            # Advance right while window still fits within duration_s
            while right + 1 < n and times[right + 1] - times[left] <= duration_s:
                right += 1
            if right >= left:
                avg = range_avg(left, right)
                if avg > best:
                    best = avg
        return round(best) if best > 0 else None

    return {
        'normalized_power': normalized_power,
        'max_5s_power':     peak_power(5),
        'max_1min_power':   peak_power(60),
        'max_5min_power':   peak_power(300),
        'max_20min_power':  peak_power(1200),
    }


# Exact Garmin track type codes → label (checked first)
GARMIN_TYPE_MAP = {
    'running':               'Running',
    'track_running':         'Running',
    'treadmill_running':     'Running',
    'trail_running':         'Trail Running',
    'hiking':                'Hiking',
    'walking':               'Walking',
    'cycling':               'Cycling',
    'road_biking':           'Cycling',
    'mountain_biking':       'Cycling',
    'indoor_cycling':        'Cycling',
    'virtual_ride':          'Cycling',
    'gravel_cycling':        'Cycling',
    'swimming':              'Swimming',
    'lap_swimming':          'Swimming',
    'open_water_swimming':   'Swimming',
    'strength_training':     'Strength',
    'cardio':                'Cardio',
    'yoga':                  'Yoga',
    'elliptical':            'Elliptical',
    'fitness_equipment':     'Gym',
    'rowing':                'Rowing',
    'indoor_rowing':         'Rowing',
    'skiing':                'Skiing',
    'ski_touring':           'Skiing',
    'cross_country_skiing':  'Skiing',
    'snowboarding':          'Snowboarding',
    'stand_up_paddleboarding': 'Paddleboarding',
    'surfing':               'Surfing',
}

# Ordered keyword fallback — more specific entries must come before broader ones
ACTIVITY_TYPE_KEYWORDS = [
    ('trail',     'Trail Running'),
    ('snowboard', 'Snowboarding'),
    ('elliptic',  'Elliptical'),
    ('strength',  'Strength'),
    ('cardio',    'Cardio'),
    ('paddle',    'Paddleboarding'),
    ('surf',      'Surfing'),
    ('row',       'Rowing'),
    ('ski',       'Skiing'),
    ('run',       'Running'),
    ('hike',      'Hiking'),
    ('walk',      'Walking'),
    ('bike',      'Cycling'),
    ('cycl',      'Cycling'),
    ('ride',      'Cycling'),
    ('swim',      'Swimming'),
    ('yoga',      'Yoga'),
    ('gym',       'Gym'),
]

def infer_type(filename: str, track_type: str | None, track_name: str | None = None) -> str:
    # 1. Exact match on Garmin track type code
    if track_type:
        label = GARMIN_TYPE_MAP.get(track_type.lower().strip())
        if label:
            return label
    # 2. Keyword scan across filename + track type + track name
    combined = ' '.join(filter(None, [filename, track_type, track_name])).lower()
    for kw, label in ACTIVITY_TYPE_KEYWORDS:
        if kw in combined:
            return label
    return 'Other'


# ── Per-file parser ───────────────────────────────────────────────────────────

def parse_file(path: Path) -> dict | None:
    with open(path, 'r', encoding='utf-8') as f:
        gpx = gpxpy.parse(f)

    points = []
    track_type = None
    track_name = None

    for track in gpx.tracks:
        track_type = track_type or track.type
        track_name = track_name or track.name
        for segment in track.segments:
            for pt in segment.points:
                points.append(pt)

    if not points:
        return None

    # Times
    times = [p.time for p in points if p.time]
    if len(times) < 2:
        return None

    start_time = times[0].astimezone(timezone.utc)
    end_time   = times[-1].astimezone(timezone.utc)
    duration_s = (end_time - start_time).total_seconds()
    if duration_s <= 0:
        return None

    # Distance & elevation
    distance_m    = 0.0
    elevation_gain = 0.0
    prev_pt = None
    for pt in points:
        if prev_pt is not None:
            if pt.latitude and pt.longitude and prev_pt.latitude and prev_pt.longitude:
                distance_m += haversine_m(prev_pt.latitude, prev_pt.longitude,
                                          pt.latitude,  pt.longitude)
            if pt.elevation and prev_pt.elevation:
                diff = pt.elevation - prev_pt.elevation
                if diff > 0:
                    elevation_gain += diff
        prev_pt = pt

    # Heart rate
    hr_values = [get_hr(p) for p in points]
    hr_values = [h for h in hr_values if h is not None]
    avg_hr = round(sum(hr_values) / len(hr_values)) if hr_values else None
    max_hr = round(max(hr_values))                  if hr_values else None

    # Cadence
    cad_values = [get_cadence(p) for p in points]
    cad_values = [c for c in cad_values if c is not None]
    avg_cadence = round(sum(cad_values) / len(cad_values)) if cad_values else None

    # Power
    pwr_values = [get_power(p) for p in points]
    pwr_values = [w for w in pwr_values if w is not None and w > 0]
    avg_power = round(sum(pwr_values) / len(pwr_values)) if pwr_values else None
    max_power = round(max(pwr_values))                   if pwr_values else None
    power_profile = compute_power_profile(points) if pwr_values else {}

    # Derived metrics
    distance_km = round(distance_m / 1000, 2)
    duration_min = round(duration_s / 60, 1)
    pace_min_km = round((duration_s / 60) / distance_km, 2) if distance_km > 0.05 else None
    speed_kmh   = round(distance_km / (duration_s / 3600), 1) if duration_s > 0 else None

    # Route (thinned to ≤500 points for the map)
    route_points = [(pt.latitude, pt.longitude) for pt in points
                    if pt.latitude and pt.longitude]
    step = max(1, len(route_points) // 100)
    route_thin = route_points[::step]

    activity_type = infer_type(path.stem, track_type, track_name)
    name = track_name or path.stem

    return {
        'file':           path.name,
        'name':           name,
        'type':           activity_type,
        'date':           start_time.strftime('%Y-%m-%d'),
        'start_time':     start_time.isoformat(),
        'duration_min':   duration_min,
        'distance_km':    distance_km,
        'pace_min_km':    pace_min_km,
        'speed_kmh':      speed_kmh,
        'elevation_gain': round(elevation_gain),
        'avg_hr':         avg_hr,
        'max_hr':         max_hr,
        'avg_cadence':    avg_cadence,
        'avg_power':        avg_power,
        'max_power':        max_power,
        'normalized_power': power_profile.get('normalized_power'),
        'max_5s_power':     power_profile.get('max_5s_power'),
        'max_1min_power':   power_profile.get('max_1min_power'),
        'max_5min_power':   power_profile.get('max_5min_power'),
        'max_20min_power':  power_profile.get('max_20min_power'),
        'num_points':       len(points),
        'route':          route_thin,     # list of [lat, lon]
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def load_existing() -> list[dict]:
    """Load previously-parsed activities from dashboard/activities.js."""
    if not OUT_JS.exists():
        return []
    text = OUT_JS.read_text(encoding='utf-8').strip()
    # Strip `const ACTIVITIES = ` prefix and trailing `;`
    start = text.find('[')
    end   = text.rfind(']')
    if start < 0 or end < 0:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def main():
    import sys
    full_rebuild = '--rebuild' in sys.argv

    files = sorted(RAW_DIR.glob('*.gpx'))
    print(f"Found {len(files)} GPX files")

    existing = [] if full_rebuild else load_existing()
    existing_by_file = {a['file']: a for a in existing}
    seen = set() if full_rebuild else load_seen()
    seen |= set(existing_by_file.keys())

    new_files = [p for p in files if p.name not in seen]
    print(f"Existing parsed: {len(existing_by_file)}   new to parse: {len(new_files)}")

    new_activities: list[dict] = []
    for path in tqdm(new_files, unit='file'):
        seen.add(path.name)
        try:
            result = parse_file(path)
            if result:
                new_activities.append(result)
        except Exception as e:
            print(f"  Error parsing {path.name}: {e}")

    # Persist seen set so files that parse to None aren't re-tried each run
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen)))

    if not new_activities and existing:
        print("No new activities; outputs unchanged.")
        return

    activities = list(existing_by_file.values()) + new_activities
    if not activities:
        print("No data parsed.")
        return

    # CSV (without route column — too large)
    df = pd.DataFrame([{k: v for k, v in a.items() if k != 'route'}
                       for a in activities])
    df = df.sort_values('date')
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"Saved {len(df)} rows → {OUT_CSV}")

    # JS data file for dashboard (includes thinned routes)
    activities_sorted = sorted(activities, key=lambda a: a['date'], reverse=True)
    js_content = "const ACTIVITIES = " + json.dumps(activities_sorted, indent=2) + ";\n"
    OUT_JS.write_text(js_content, encoding='utf-8')
    print(f"Saved JS data → {OUT_JS} ({len(new_activities)} new)")


if __name__ == '__main__':
    main()
