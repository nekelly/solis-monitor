"""Web dashboard for the Solis S6 inverter.

A background poller (poller.py) reads the inverter every POLL_SECONDS and saves each reading to
SQLite (store.py). The page only ever shows the latest stored reading, so the number of open
browsers has no effect on the data logger.
"""

import math
import os
import sqlite3
import sys
import time

from flask import Flask, jsonify, render_template, request

from poller import Poller
from inverter import ENERGY_LABELS, VERSION
from store import Store


def int_from_env(name, default, minimum):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(minimum, int(value))
    except ValueError:
        print(f"{name}={value!r} is not a whole number; using {default}", file=sys.stderr)
        return default


# Both optional: without SOLIS_HOST the poller scans for the logger; without SCAN_SUBNET it scans the
# network the logger's last known address is on
SOLIS_HOST = os.environ.get("SOLIS_HOST", "").strip() or None
SCAN_SUBNET = os.environ.get("SCAN_SUBNET", "").strip() or None
PAGE_TITLE = os.environ.get("PAGE_TITLE", "").strip() or "Solis S6"
POLL_SECONDS = int_from_env("POLL_SECONDS", 60, minimum=5)
RETENTION_DAYS = int_from_env("RETENTION_DAYS", 365, minimum=0)  # 0 keeps readings forever
DB_PATH = os.environ.get("DB_PATH", "").strip() or "solis.db"
# An address found by scanning is remembered next to the database (in Docker: ./data on the host)
ADDRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "logger_address.json")

try:
    store = Store(DB_PATH)
except sqlite3.Error as e:
    sys.exit(f"Can't open the database at {DB_PATH}: {e}. "
             "Check the folder exists and is writable by the app (in Docker: owned by uid 1000).")

# Started at import, so there must only be one process running this app (gunicorn uses one worker)
poller = Poller(SOLIS_HOST, SCAN_SUBNET, ADDRESS_FILE, store, POLL_SECONDS, RETENTION_DAYS)
poller.start()

# index.html sits next to this file rather than in Flask's default "templates" folder
app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)))


@app.get("/")
def index():
    return render_template("index.html", page_title=PAGE_TITLE, version=VERSION, energy_labels=ENERGY_LABELS)


@app.get("/api/data")
def api_data():
    snapshot = poller.snapshot()
    now = time.time()
    next_poll_at = snapshot.pop("next_poll_at")
    return jsonify(
        **snapshot,
        # Relative times, so a difference between the browser's and server's clocks doesn't matter
        age_seconds=None if snapshot["taken_at"] is None else round(now - snapshot["taken_at"]),
        seconds_until_next_poll=None if next_poll_at is None else max(0, round(next_poll_at - now)),
        poll_seconds=POLL_SECONDS,
    )


# Graph time ranges, in seconds
HISTORY_RANGES = {"6h": 6 * 3600, "12h": 12 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
# The page asks for about as many points as fit its graph width; these limit what it can ask for
MIN_POINTS = 20
MAX_POINTS = 1500


def bucket_seconds(span, points):
    """Averaging step for a graph range: a whole number of poll intervals.

    If the range has no more readings than the points wanted, that's one poll interval, so every
    reading is shown. Otherwise it's the smallest multiple of the poll interval that gives at most
    that many points, which also means each bucket holds the same number of readings (polls are
    aligned to the interval).
    """
    return POLL_SECONDS * max(1, math.ceil(span / points / POLL_SECONDS))


@app.get("/api/history")
def api_history():
    field = request.args.get("field", "")
    range_key = request.args.get("range", "24h")
    if range_key not in HISTORY_RANGES:
        return jsonify(error=f"range must be one of {', '.join(HISTORY_RANGES)}"), 400
    if field not in store.columns():
        return jsonify(error=f"unknown field {field!r}"), 404

    span = HISTORY_RANGES[range_key]
    end = int(time.time()) + 1
    start = end - span
    try:
        points_wanted = int(request.args.get("points", MAX_POINTS))
    except ValueError:
        return jsonify(error="points must be a whole number"), 400
    points_wanted = min(MAX_POINTS, max(MIN_POINTS, points_wanted))
    bucket = bucket_seconds(span, points_wanted)
    points, peak, low = store.history(field, start, end, bucket)
    return jsonify(
        field=field,
        range=range_key,
        start=start,
        end=end,
        bucket_seconds=bucket,
        poll_seconds=POLL_SECONDS,
        points=[[ts, round(value, 2)] for ts, value in points],
        peak=None if peak is None else [peak[0], round(peak[1], 2)],
        low=None if low is None else [low[0], round(low[1], 2)],
    )


@app.get("/healthz")
def healthz():
    # Doesn't touch the inverter, so the container stays healthy when the logger is offline;
    # it does report a problem if the polling thread has died
    if not poller.is_alive():
        return "poller stopped", 503
    return "ok"


if __name__ == "__main__":
    # Development server for running outside Docker; the container uses gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
