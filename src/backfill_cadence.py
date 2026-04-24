"""Read each parquet, compute mean cadence (cadence >= 55 rpm to exclude coasting and sensor drops), write dashboard/cadence_activities.js."""
from pathlib import Path
import json
import pandas as pd

ROOT = Path('/Users/Pau/Documents/GitHub/Sports-Analytics')
PARQUET_DIR = ROOT / 'data' / 'processed' / 'cycling_power'
OUT_JS = ROOT / 'dashboard' / 'cadence_activities.js'

out = {}
files = sorted(PARQUET_DIR.glob('*.parquet'))
for p in files:
    aid = int(p.stem)
    try:
        df = pd.read_parquet(p, columns=['cadence_rpm'])
    except Exception:
        continue
    if 'cadence_rpm' not in df.columns:
        continue
    s = df['cadence_rpm'].dropna()
    s_pedal = s[s >= 55]
    if len(s_pedal) == 0:
        continue
    out[aid] = round(float(s_pedal.mean()), 1)

OUT_JS.parent.mkdir(parents=True, exist_ok=True)
OUT_JS.write_text('window.CYCLING_CADENCE = ' + json.dumps(out) + ';\n')
print(f'Wrote cadence for {len(out)} activities (of {len(files)} parquets).')
