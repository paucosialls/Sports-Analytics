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
import hashlib
import hmac
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
SYNC_SH = ROOT / "daily_sync.sh"
LOG_PATH = ROOT / "logs" / "daily.log"
PORT = int(os.getenv("PORT", "8765"))
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8766"))
SYNC_TIMES = ((7, 20), (10, 20), (13, 20), (16, 20), (19, 20), (22, 20))

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


def _start_sync() -> bool:
    """Start one sync unless another manual or scheduled run is active."""
    with STATE_LOCK:
        if STATE["running"]:
            return False
        STATE["running"] = True
        STATE["started_at"] = int(time.time())
        STATE["finished_at"] = 0
        STATE["exit_code"] = None
    threading.Thread(target=_run_sync, daemon=True).start()
    return True


def _seconds_until_next_sync(now: datetime | None = None) -> float:
    """Return seconds until the next fixed local-time refresh slot."""
    current = now or datetime.now()
    for hour, minute in SYNC_TIMES:
        candidate = current.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate > current:
            return (candidate - current).total_seconds()
    tomorrow = current + timedelta(days=1)
    first_hour, first_minute = SYNC_TIMES[0]
    candidate = tomorrow.replace(
        hour=first_hour, minute=first_minute, second=0, microsecond=0
    )
    return (candidate - current).total_seconds()


def _scheduled_sync_loop() -> None:
    """Run at fixed daytime slots; manual refreshes share the same lock."""
    while True:
        time.sleep(_seconds_until_next_sync())
        _start_sync()


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

    def end_headers(self) -> None:
        # Dashboard JS files contain changing Garmin data. Do not let an
        # installed browser shortcut keep yesterday's health or workout view.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

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
            if not _start_sync():
                self._json(409, {"error": "already running", **STATE})
                return
            self._json(202, self._state_snapshot())
            return
        self.send_error(405, "Method not allowed")


def _gateway_token() -> str:
    """Derive a dedicated gateway secret without exporting the GitHub token."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("GH_TOKEN="):
            github_token = raw_line.split("=", 1)[1].strip().strip("'\"")
            return hashlib.sha256(
                ("sports-refresh-gateway-v1:" + github_token).encode("utf-8")
            ).hexdigest()
    return ""


class GatewayHandler(Handler):
    """Token-protected refresh API exposed through Tailscale Funnel."""

    def _authorized(self) -> bool:
        expected = _gateway_token()
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def _reject_unless_authorized(self) -> bool:
        if self._authorized():
            return False
        self._json(401, {"error": "unauthorized"})
        return True

    def _state_snapshot(self) -> dict:
        # Do not expose the local sync log on the public gateway.
        with STATE_LOCK:
            return dict(STATE)

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_unless_authorized():
            return
        if self.path.rstrip("/") in ("/refresh/status", "/refresh-status"):
            self._json(200, self._state_snapshot())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_unless_authorized():
            return
        super().do_POST()


def main() -> None:
    gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), GatewayHandler)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    threading.Thread(target=_scheduled_sync_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(
        f"sports-dashboard server on http://127.0.0.1:{PORT}; "
        f"refresh gateway on http://127.0.0.1:{GATEWAY_PORT}; "
        "automatic sync at 07:20, 10:20, 13:20, 16:20, 19:20 and 22:20"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
