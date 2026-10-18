#!/usr/bin/env python3
"""
Exaroton scheduler: keeps the server up 8:00 PM - 11:00 PM Eastern.

Run it every few minutes (cron / Task Scheduler). Each run checks the server once
and exits. Rules:
  * Server is offline during the window  -> script starts it (and remembers it did).
  * Server is already online/starting    -> script does nothing.
  * 11 PM, server was started BY THE SCRIPT -> script stops it.
  * 11 PM, server was started by anyone else (admin) -> script leaves it alone.
  * Admin stops the server during the window -> script will NOT restart it until
    the next day's window (so maintenance isn't fought).
  * Create a file named keep_alive.flag next to this script to block the 11 PM stop.

Usage:
  export EXAROTON_TOKEN="your-api-token"
  export EXAROTON_SERVER_ID="your-server-id"
  python exaroton_scheduler.py            # normal run
  python exaroton_scheduler.py --dry-run  # log what it WOULD do, change nothing
  python exaroton_scheduler.py --list     # print your servers + IDs
"""
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------- settings --------------------------------------
API = "https://api.exaroton.com/v1"
TOKEN = os.environ.get("EXAROTON_TOKEN", "")
SERVER_ID = os.environ.get("EXAROTON_SERVER_ID", "")

TZ = ZoneInfo("America/New_York")   # handles EST/EDT switching automatically
OPEN_HOUR = 20                      # 8:00 PM  (server may start)
CLOSE_HOUR = 23                     # 11:00 PM (script-started server stops)
RESTART_ON_CRASH = False            # True = also start the server if it crashed
GRACE_SECONDS = 180                 # ignore API lag right after we start/stop

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "scheduler_state.json"
KEEP_ALIVE_FILE = BASE_DIR / "keep_alive.flag"
LOG_FILE = BASE_DIR / "scheduler.log"
# -----------------------------------------------------------------------------

# Exaroton status codes
OFFLINE, ONLINE, STARTING, STOPPING, RESTARTING, SAVING, LOADING, CRASHED, PENDING, PREPARING = range(10)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("exaroton")


def api(path):
    """GET an Exaroton endpoint and return its `data` payload."""
    r = requests.get(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"Exaroton API error: {body.get('error')}")
    return body.get("data")


def now():
    return datetime.now(TZ)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def run(dry_run=False):
    t = now()
    in_window = OPEN_HOUR <= t.hour < CLOSE_HOUR
    today = t.date().isoformat()

    status = api(f"/servers/{SERVER_ID}/")["status"]
    state = load_state()
    last = state.get("last_status")
    recent = time.time() - state.get("last_action_ts", 0) < GRACE_SECONDS

    # Server went ONLINE -> stopping/offline inside the window, and not by us:
    # an admin (or Exaroton, e.g. out of credits) stopped it. Pause until tomorrow.
    if in_window and last == ONLINE and status in (STOPPING, OFFLINE) and not recent:
        log.info("Server was stopped during the window by someone else; pausing auto-start for today.")
        state["paused_date"] = today
        state["script_started"] = False

    if in_window:
        paused = state.get("paused_date") == today
        if status == OFFLINE and not paused and not recent:
            log.info("In window and server is offline -> starting it.")
            if not dry_run:
                api(f"/servers/{SERVER_ID}/start/")
                state["script_started"] = True
                state["last_action_ts"] = time.time()
        elif status == CRASHED and RESTART_ON_CRASH and not paused and not recent:
            log.warning("Server crashed -> restarting.")
            if not dry_run:
                api(f"/servers/{SERVER_ID}/start/")
                state["script_started"] = True
                state["last_action_ts"] = time.time()
        elif status == OFFLINE and paused:
            state["script_started"] = False
            log.info("Offline but auto-start is paused for today.")
        else:
            log.info("In window, status=%s, nothing to do.", status)
    else:
        if status == ONLINE and state.get("script_started"):
            if KEEP_ALIVE_FILE.exists():
                log.info("Window closed, but keep_alive.flag exists -> leaving server up.")
            else:
                log.info("Window closed and script started this session -> stopping.")
                if not dry_run:
                    api(f"/servers/{SERVER_ID}/stop/")
                    state["script_started"] = False
                    state["last_action_ts"] = time.time()
        elif status == ONLINE:
            log.info("Window closed, server was started manually -> leaving it alone.")
        elif status == OFFLINE:
            state["script_started"] = False
            log.info("Window closed, server offline, nothing to do.")
        else:
            log.info("Window closed, status=%s, nothing to do.", status)

    state["last_status"] = status
    if not dry_run:
        save_state(state)


def main():
    if not TOKEN:
        sys.exit("Set the EXAROTON_TOKEN environment variable.")
    if "--list" in sys.argv:
        for s in api("/servers/"):
            print(f"{s['name']}  ->  {s['id']}  (status {s['status']})")
        return
    if not SERVER_ID:
        sys.exit("Set EXAROTON_SERVER_ID (run with --list to find it).")
    try:
        run(dry_run="--dry-run" in sys.argv)
    except Exception:
        log.exception("Run failed; will retry on the next scheduled run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
