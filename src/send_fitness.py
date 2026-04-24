"""
send_fitness.py — Build a standalone mobile-friendly fitness/fatigue HTML
and email it via Mail.app. One-file attachment: you tap it in the Mail app on
your phone, it opens in the browser, renders fully (Chart.js from CDN).

Usage:  python src/send_fitness.py [recipient@email.com]
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FITNESS_JS = ROOT / "dashboard" / "fitness.js"
OUT_HTML = ROOT / "out" / "fitness_mobile.html"
DEFAULT_RECIPIENT = "pau.cosialls.guillen@gmail.com"


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fitness & Fatigue — __DATE__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg:#0f1117; --surface:#1a1d27; --border:#2a2d3a; --accent:#4f8ef7; --accent2:#f74f8e; --green:#4fbd87; --yellow:#f7c94f; --text:#e2e4ed; --muted:#6b7080; }
body { font-family:-apple-system,BlinkMacSystemFont,'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; padding:14px; }
header { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:12px; flex-wrap:wrap; gap:8px; }
h1 { font-size:1.1rem; font-weight:700; }
.sub { color:var(--muted); font-size:.78rem; }
.cards { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:14px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px; text-align:center; }
.card .label { color:var(--muted); font-size:.65rem; text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px; }
.card .value { font-size:1.5rem; font-weight:700; line-height:1; }
.card.fitness .value { color:var(--accent); }
.card.fatigue .value { color:var(--accent2); }
.card.form .value { color:var(--green); }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px; margin-bottom:12px; }
.chart-wrap { height:62vh; min-height:340px; position:relative; }
.controls { display:flex; gap:4px; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:3px; margin-bottom:10px; justify-content:center; }
.controls button { flex:1; background:none; border:none; color:var(--muted); font-size:.75rem; font-weight:600; letter-spacing:.04em; text-transform:uppercase; padding:6px 10px; border-radius:5px; cursor:pointer; }
.controls button.active { background:var(--accent); color:#fff; }
.muted { color:var(--muted); font-size:.75rem; line-height:1.5; }
.muted strong { color:var(--text); }
</style>
</head>
<body>
<header>
  <h1>Fitness &amp; Fatigue</h1>
  <span class="sub" id="generatedAt"></span>
</header>

<section class="cards">
  <div class="card fitness"><div class="label">Fitness (CTL)</div><div class="value" id="cardCtl">—</div></div>
  <div class="card fatigue"><div class="label">Fatigue (ATL)</div><div class="value" id="cardAtl">—</div></div>
  <div class="card form"><div class="label">Form (TSB)</div><div class="value" id="cardTsb">—</div></div>
</section>

<div class="panel">
  <div class="controls" id="rangeCtrls">
    <button data-days="30">30d</button>
    <button data-days="90">90d</button>
    <button data-days="180">6m</button>
    <button data-days="365">1y</button>
    <button data-days="0" class="active">All</button>
  </div>
  <div class="chart-wrap"><canvas id="fitnessChart"></canvas></div>
</div>

<div class="panel muted">
  <strong>CTL</strong> = 42-day chronic training load (fitness). <strong>ATL</strong> = 7-day acute load (fatigue). <strong>TSB</strong> = form = yesterday's CTL − ATL.<br>
  Cycling with power → precomputed TSS. Other activities → hrTSS (TRIMP normalized to 100 per hour at LTHR).<br>
  Thresholds derived from your data: <span id="thr"></span>.
</div>

<script>
__DATA_SCRIPT__
</script>
<script>
const data = window.fitnessData;
document.getElementById('generatedAt').textContent =
  'updated ' + (data.generated_at || '').replace('T', ' ').slice(0, 16);
const last = data.daily[data.daily.length - 1];
document.getElementById('cardCtl').textContent = last.ctl.toFixed(1);
document.getElementById('cardAtl').textContent = last.atl.toFixed(1);
const sign = last.tsb >= 0 ? '+' : '';
document.getElementById('cardTsb').textContent = sign + last.tsb.toFixed(1);
const t = data.thresholds || {};
document.getElementById('thr').textContent =
  'HRmax ' + t.hr_max + ', resting ' + t.hr_rest + ', LTHR ' + t.lthr;

const actsByDate = new Map();
for (const a of data.activities) {
  if (!actsByDate.has(a.date)) actsByDate.set(a.date, []);
  actsByDate.get(a.date).push(a);
}

let chart;
function render(days) {
  let rows = data.daily;
  if (days > 0) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    rows = rows.filter(r => new Date(r.date) >= cutoff);
  }
  const labels = rows.map(r => r.date);
  if (chart) chart.destroy();
  const ctx = document.getElementById('fitnessChart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { type:'bar', label:'Daily load', data: rows.map(r=>r.tss),
          backgroundColor:'rgba(107,112,128,0.35)', borderWidth:0, yAxisID:'y1', order:4 },
        { type:'line', label:'CTL — Fitness', data: rows.map(r=>r.ctl),
          borderColor:'#4f8ef7', backgroundColor:'rgba(79,142,247,0.15)',
          borderWidth:2.2, fill:true, pointRadius:0, tension:0.25, yAxisID:'y', order:1 },
        { type:'line', label:'ATL — Fatigue', data: rows.map(r=>r.atl),
          borderColor:'#f74f8e', borderWidth:1.8, fill:false, pointRadius:0,
          tension:0.25, yAxisID:'y', order:2 },
        { type:'line', label:'TSB — Form', data: rows.map(r=>r.tsb),
          borderColor:'#4fbd87', borderWidth:1.6, borderDash:[5,4], fill:false,
          pointRadius:0, tension:0, yAxisID:'y', order:3 },
      ],
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:'index', intersect:false },
      scales: {
        x: { type:'time', time:{ unit:days<=90?'week':'month' },
             grid:{ color:'rgba(255,255,255,0.04)' },
             ticks:{ color:'#6b7080', maxTicksLimit:8, font:{size:10} } },
        y: { position:'left', grid:{ color:'rgba(255,255,255,0.05)' },
             ticks:{ color:'#6b7080', font:{size:10} } },
        y1:{ position:'right', grid:{ display:false }, beginAtZero:true,
             ticks:{ color:'#6b7080', font:{size:10} } },
      },
      plugins: {
        legend:{ labels:{ color:'#e2e4ed', boxWidth:10, font:{size:10} } },
        tooltip:{ callbacks: { afterBody:(items)=>{
          if (!items.length) return '';
          const d = items[0].label.slice(0,10);
          const acts = actsByDate.get(d) || [];
          if (!acts.length) return '';
          return ['', ...acts.map(a=>'• '+a.name+'  ['+a.method+']  '+a.duration_min.toFixed(0)+'min  load '+a.load.toFixed(0))];
        } } },
      },
    },
  });
}
document.querySelectorAll('#rangeCtrls button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#rangeCtrls button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    render(parseInt(btn.dataset.days, 10));
  });
});
render(0);
</script>
</body>
</html>
"""


def build_standalone(out_path: Path) -> None:
    data_script = FITNESS_JS.read_text()
    html = TEMPLATE.replace("__DATE__", date.today().isoformat()).replace(
        "__DATA_SCRIPT__", data_script)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(html)


def send_via_mail(recipient: str, file_path: Path) -> None:
    today = date.today().strftime("%d %b %Y")
    subject = f"Fitness & Fatigue — {today}"
    body = ("Hola,\\n\\nAdjunto el dashboard de fitness/fatigue "
            f"actualizado a {today}. Tap el HTML adjunto para verlo en el "
            "navegador.\\n\\nPau")
    script = f'''
set recipientAddress to "{recipient}"
set filePath to POSIX file "{file_path}"
set theSubject to "{subject}"
set theBody to "{body}"

tell application "Mail"
  set newMsg to make new outgoing message with properties {{subject:theSubject, content:theBody, visible:false}}
  tell newMsg
    make new to recipient at end of to recipients with properties {{address:recipientAddress}}
    make new attachment with properties {{file name:filePath}} at after last paragraph
  end tell
  send newMsg
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True)


def main() -> int:
    recipient = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RECIPIENT
    if not FITNESS_JS.exists():
        print(f"fitness.js not found at {FITNESS_JS}. "
              "Run src/fitness_fatigue.py first.", file=sys.stderr)
        return 1
    build_standalone(OUT_HTML)
    print(f"Built {OUT_HTML} ({OUT_HTML.stat().st_size // 1024} KB)")
    send_via_mail(recipient, OUT_HTML)
    print(f"Sent to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
