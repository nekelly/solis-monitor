"""Background thread that reads the inverter on a fixed interval and saves each reading.

It also keeps track of the data logger's address: if the logger stops answering (or no address is
configured), it scans the network for it and remembers the address it finds in a small JSON file.
"""

import json
import os
import sqlite3
import sys
import threading
import time
import traceback

from inverter import find_inverters, local_subnet, read_data, subnet_of

PRUNE_EVERY_SECONDS = 24 * 3600
STRING_COLUMNS = ["pv1_power", "pv2_power"]
FAILURES_BEFORE_SCAN = 3          # failed polls in a row before looking for the logger elsewhere
MIN_SECONDS_BETWEEN_SCANS = 600   # so an offline logger doesn't cause a scan on every poll


def log(message):
    print(f"poller: {message}", file=sys.stderr, flush=True)


class Poller:
    def __init__(self, configured_host, scan_subnet, address_file, store, interval_seconds, retention_days):
        """configured_host: SOLIS_HOST, or None. scan_subnet: SCAN_SUBNET, or None to use the network
        the logger's address is on. address_file: where an address found by scanning is remembered."""
        self.configured_host = configured_host
        self.scan_subnet = scan_subnet
        self.address_file = address_file
        self.host = self._starting_host()
        self.store = store
        self.interval = interval_seconds
        self.retention_days = retention_days
        self._failures = 0
        self._last_scan = None     # time.monotonic() of the last scan
        self._last_subnet = None   # the network that scan searched
        self._lock = threading.Lock()
        self._latest = None        # most recent successful read_data() result
        self._previous = None      # the successful reading before that, for the page's rise/fall arrows
        self._latest_ts = None     # when it was taken (Unix seconds)
        self._last_error = None    # message from the most recent failed poll, cleared on success
        self._pv_energy_today = None   # estimated kWh per PV string since the inverter's midnight
        self._next_poll_ts = None
        self._last_prune = 0.0
        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def snapshot(self):
        """The latest reading and poll status, for the web page."""
        with self._lock:
            return {
                "reading": self._latest,
                "previous": self._previous,
                "pv_energy_today": self._pv_energy_today,
                "taken_at": self._latest_ts,
                "error": self._last_error,
                "next_poll_at": self._next_poll_ts,
                "host": self.host,
            }

    # ---------- finding the data logger ----------

    def _starting_host(self):
        """An address found by an earlier scan wins, unless SOLIS_HOST has been changed since then
        (the file records which SOLIS_HOST it replaced, so a deliberate change isn't overridden)."""
        try:
            with open(self.address_file, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("configured") == self.configured_host and saved.get("found"):
                log(f"using {saved['found']}, found by an earlier scan (from {self.address_file})")
                return saved["found"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"ignoring {self.address_file}: {e}")
        return self.configured_host

    def _save_found_host(self, host):
        # Write to a temporary file and rename, so a crash mid-write can't leave a broken file
        temporary = self.address_file + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump({"configured": self.configured_host, "found": host,
                           "found_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            os.replace(temporary, self.address_file)
        except OSError as e:
            log(f"couldn't save the logger's address to {self.address_file}: {e}")

    def _subnet_to_scan(self):
        if self.scan_subnet:
            return self.scan_subnet
        known = self.host or self.configured_host
        if known:
            return subnet_of(known)
        # Last resort: inside Docker this is Docker's internal network, so set SOLIS_HOST or SCAN_SUBNET
        return local_subnet()

    def _scan(self):
        """Look for the logger on the network; switch to it (and remember it) if it's moved."""
        if self._last_scan is not None and time.monotonic() - self._last_scan < MIN_SECONDS_BETWEEN_SCANS:
            return
        self._last_scan = time.monotonic()
        try:
            subnet = self._last_subnet = self._subnet_to_scan()
            log(f"scanning {subnet} for the data logger")
            found = find_inverters(subnet)
        except (OSError, ValueError) as e:
            log(f"scan failed: {e}")
            return
        if not found:
            log(f"no data logger found on {subnet}; will keep trying {self.host or 'to find one'}")
            return
        if self.host in found:
            log(f"data logger is still at {self.host}")
            return
        new_host = found[0]
        if len(found) > 1:
            log(f"several data loggers found ({', '.join(found)}); using {new_host}")
        log(f"data logger found at {new_host}" + (f" (was {self.host})" if self.host else ""))
        self.host = new_host
        self._failures = 0
        self._save_found_host(new_host)

    # ---------- polling ----------

    def _run(self):
        log(f"reading {self.host or '(address not set: will scan)'} every {self.interval} s")
        while True:
            try:
                self._poll_once()
                self._prune_if_due()
            except Exception:  # never let an unexpected error stop polling
                log("unexpected error:\n" + traceback.format_exc())
            # Poll on whole multiples of the interval (e.g. every minute on :00) so readings line up neatly in graphs
            next_ts = (int(time.time()) // self.interval + 1) * self.interval
            with self._lock:
                self._next_poll_ts = next_ts
            time.sleep(max(0.0, next_ts - time.time()))

    def _poll_once(self):
        if self.host is None:
            self._scan()
            if self.host is None:
                with self._lock:
                    self._last_error = ("No data logger address: SOLIS_HOST isn't set and scanning "
                                        f"{self._last_subnet or 'the network'} didn't find one")
                return

        ts = int(time.time())
        try:
            data = read_data(self.host)
        except (OSError, RuntimeError) as e:
            message = f"Can't read inverter at {self.host}: {e}"
            log(message)
            with self._lock:
                self._last_error = message
            self._failures += 1
            if self._failures >= FAILURES_BEFORE_SCAN:
                self._scan()   # it may have a new address
            return

        self._failures = 0
        with self._lock:
            self._previous = self._latest
            self._latest, self._latest_ts, self._last_error = data, ts, None
        try:
            self.store.insert(ts, data)
        except sqlite3.Error as e:
            # The page still shows the live reading; only the history misses this row
            log(f"couldn't save reading to {self.store.path}: {e}")
        self._update_pv_energy(ts, data)

    def _update_pv_energy(self, ts, data):
        """Recalculate each string's estimated energy since midnight (about 1,440 rows a day: cheap)."""
        # Midnight comes from the inverter's clock, which also defines its own "today" counters,
        # so PV1 + PV2 lines up with "Solar today"; the container's clock is usually UTC
        try:
            clock = data["inverter_time"].split()[1]
            hours, minutes, seconds = (int(part) for part in clock.split(":"))
        except (KeyError, IndexError, ValueError):
            return
        midnight = ts - (hours * 3600 + minutes * 60 + seconds)
        try:
            energy = self.store.energy_since(STRING_COLUMNS, midnight, max_gap_seconds=max(600, 3 * self.interval))
        except (sqlite3.Error, KeyError) as e:
            log(f"couldn't calculate today's string energy: {e}")
            return
        with self._lock:
            self._pv_energy_today = {column: round(kwh, 2) for column, kwh in energy.items()}

    def _prune_if_due(self):
        if self.retention_days <= 0 or time.time() - self._last_prune < PRUNE_EVERY_SECONDS:
            return
        self._last_prune = time.time()
        cutoff = int(time.time()) - self.retention_days * 24 * 3600
        try:
            removed = self.store.prune(cutoff)
        except sqlite3.Error as e:
            log(f"couldn't delete old readings: {e}")
            return
        if removed:
            log(f"deleted {removed} readings older than {self.retention_days} days")
