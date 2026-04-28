"""Tiny HTTP server for the Sports-Analytics dashboard.

Serves static files from `dashboard/` AND exposes:

  POST /refresh        kicks off `daily_sync.sh` (one at a time). Returns
                       JSON {state, started_at}.
  GET  /refresh/status returns JSON with current state, last-run epoch,
                       last-exit code, and tail of the daily log.

Run via systemd so it's always available on the tailnet at port 8765.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
SYNC_SH = ROOT / "daily_sync.sh"
LOG_PATH = ROOT / "logs" / "daily.log"
PORT = int(os.getenv("PORT", "8765"))

# In-memory state shared across threads.
STATE: dict = {
    "running": False,
    "started_at": 0,
    "finished_at": 0,
    "exit_code": None,
}
STATE_LOCK = threading.Lock()


def _run_sync() -> None:
    """Run daily_sync.sh once, capturing its exit code."""
    try:
        proc = subprocess.run(
            ["bash", str(SYNC_SH)],
            cwd=str(ROOT),
            capture_output=False,  # daily_sync.sh writes to its own log
            timeout=30 * 60,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except Exception:
        rc = 1
    with STATE_LOCK:
        STATE["running"] = False
        STATE["finished_at"] = int(time.time())
        STATE["exit_code"] = rc


def _tail(path: Path, max_bytes: int = 4_000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", errors="replace")


class Handler(SimpleHTTPRequestHandler):
    # Serve from dashboard/ regardless of cwd.
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    # Quiet down the per-request log noise; systemd journal still gets errors.
    def log_message(self, fmt, *args) -> None:  # noqa: D401
        return

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _state_snapshot(self) -> dict:
        with STATE_LOCK:
            snap = dict(STATE)
        snap["log_tail"] = _tail(LOG_PATH)
        return snap

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/refresh/status", "/refresh-status"):
            self._json(200, self._state_snapshot())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/refresh":
            with STATE_LOCK:
                if STATE["running"]:
                    self._json(409, {"error": "already running", **STATE})
                    return
                STATE["running"] = True
                STATE["started_at"] = int(time.time())
                STATE["finished_at"] = 0
                STATE["exit_code"] = None
            threading.Thread(target=_run_sync, daemon=True).start()
            self._json(202, self._state_snapshot())
            return
        self.send_error(405, "Method not allowed")


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"sports-dashboard server on http://0.0.0.0:{PORT}  (root={DASHBOARD})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
