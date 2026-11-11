#!/usr/bin/env python3

# Author: Noel Kelly

"""Command-line tool for the Solis S6 inverter: live readings, looping, CSV logging and summaries.

The inverter-reading code lives in inverter.py, which the web app shares.
"""

import argparse
import csv
import io
import math
import os
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace

from inverter import ENERGY_LABELS, VERSION, find_inverters, local_subnet, read_data


def main():
    parser = argparse.ArgumentParser(description="Show live data from a Solis inverter over Modbus TCP.")
    parser.add_argument("--version", action="version", version=f"%(prog)s v{VERSION}")
    parser.add_argument("--ip", help="IP address of the data logger (without it, the local network is scanned)")
    parser.add_argument("--loop", type=positive_seconds, metavar="N", help="fetch and show data every N seconds until Ctrl+C")
    parser.add_argument("--csv", metavar="FILE", help="append timestamp, PV1, PV2, total solar, battery and grid power to a CSV file")
    parser.add_argument("--summary", action="store_true", help="show daily PV1/PV2 energy worked out from the --csv file, then exit")
    args = parser.parse_args()

    if args.summary:
        if not args.csv:
            parser.error("--summary needs --csv FILE to say which log to summarise")
        if args.ip or args.loop is not None:
            parser.error("--summary only reads the CSV log and can't be combined with --ip or --loop")
        show_summary(args.csv)
        return

    host = args.ip or scan_for_logger()

    if args.loop is None:
        values = show_data(host)
        if args.csv:
            log_csv(args.csv, values)
        return

    try:
        while True:
            started = time.monotonic()
            # Capture the output first so the screen is only cleared once new data is ready
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    values = show_data(host)
            except (OSError, RuntimeError) as e:
                print(f"{time.strftime('%H:%M:%S')} error reading inverter: {e}", file=sys.stderr)
            else:
                clear_screen()
                print(output.getvalue(), end="", flush=True)
                if args.csv:
                    log_csv(args.csv, values)
            wait_with_countdown(started + args.loop)
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            print()  # move past the countdown line


def wait_with_countdown(deadline):
    # Only show the countdown on a terminal; when redirected to a file it would just add clutter
    if not sys.stdout.isatty():
        time.sleep(max(0, deadline - time.monotonic()))
        return

    message = ""
    print()
    while (remaining := deadline - time.monotonic()) > 0:
        seconds_left = math.ceil(remaining)
        message = f"Next update in {seconds_left} s (Ctrl+C to stop)"
        print(f"\r{message}  ", end="", flush=True)
        # Sleep until the displayed number should change
        time.sleep(remaining - (seconds_left - 1))
    print("\r" + " " * (len(message) + 2) + "\r", end="", flush=True)


CSV_HEADER = ["timestamp", "pv1_power_w", "pv2_power_w", "total_power_w", "battery_power_w", "grid_power_w"]


def log_csv(path, values):
    # Errors are reported rather than raised so that, e.g., the file being open in Excel doesn't stop a --loop run
    try:
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        if not write_header:
            with open(path, newline="", encoding="utf-8-sig") as f:
                existing_header = next(csv.reader(f), [])
            if existing_header != CSV_HEADER:
                print(f"{time.strftime('%H:%M:%S')} not writing {path}: its columns don't match "
                      f"({','.join(CSV_HEADER)}); use a new file", file=sys.stderr)
                return
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_HEADER)
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                values["pv1_power"],
                values["pv2_power"],
                values["solar_power"],
                values["battery_power"],
                values["grid_power"],
            ])
    except OSError as e:
        print(f"{time.strftime('%H:%M:%S')} error writing {path}: {e}", file=sys.stderr)


# Readings further apart than this are treated as a gap in logging rather than bridged
MAX_GAP_SECONDS = 600


def show_summary(path):
    readings = []
    try:
        # utf-8-sig ignores the byte-order mark Excel adds when saving as "CSV UTF-8"
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    readings.append((
                        datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S"),
                        float(row["pv1_power_w"]),
                        float(row["pv2_power_w"]),
                    ))
                except (KeyError, TypeError, ValueError):
                    pass  # skip malformed rows
    except OSError as e:
        sys.exit(f"Can't read {path}: {e}")
    if len(readings) < 2:
        sys.exit(f"{path} needs at least two readings to work out energy")
    readings.sort()

    # Per day: [pv1 kWh, pv2 kWh, seconds covered]
    days = {}
    for (t1, pv1_a, pv2_a), (t2, pv1_b, pv2_b) in zip(readings, readings[1:]):
        seconds = (t2 - t1).total_seconds()
        if seconds <= 0 or seconds > MAX_GAP_SECONDS:
            continue
        hours = seconds / 3600
        # Trapezoidal rule: average power over the interval x time, converted W·h -> kWh
        day = days.setdefault(t1.date(), [0.0, 0.0, 0.0])
        day[0] += (pv1_a + pv1_b) / 2 * hours / 1000
        day[1] += (pv2_a + pv2_b) / 2 * hours / 1000
        day[2] += seconds

    if not days:
        sys.exit(f"No readings in {path} are within {MAX_GAP_SECONDS} s of each other")

    print(f"{'Date':12}{'PV1':>10}{'PV2':>10}{'PV1+PV2':>10}{'Logged':>9}")
    for date, (pv1, pv2, seconds) in sorted(days.items()):
        print(f"{date.isoformat():12}{pv1:>6.2f} kWh{pv2:>6.2f} kWh{pv1 + pv2:>6.2f} kWh{seconds / 3600:>7.1f} h")
    print()
    print(f"Energy is estimated from logged power readings; gaps over {MAX_GAP_SECONDS // 60} minutes are not counted.")


def positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number of seconds")
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return seconds


def clear_screen():
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    else:
        print()  # when redirected to a file, separate readings with a blank line


def show_data(host):
    data = read_data(host)
    d = SimpleNamespace(**data)

    print(f"Time:          {d.inverter_time}")
    print()
    print(f"Solar:         {d.solar_power} W")
    print(f"  PV1:         {d.pv1_power} W  ({d.pv1_voltage} V  {d.pv1_current} A)")
    print(f"  PV2:         {d.pv2_power} W  ({d.pv2_voltage} V  {d.pv2_current} A)")
    print(f"Inverter AC:   {d.inverter_ac_power} W")
    print(f"House load:    {d.house_load_power} W")
    print(f"Battery:       {d.battery_power:+} W ({'discharging' if d.battery_discharging else 'charging'})")
    print(f"  Charge:      {d.battery_soc}%  (health {d.battery_health}%)")
    print(f"  Voltage:     {d.battery_voltage} V")
    print(f"Grid:          {d.grid_power:+} W ({'exporting' if d.grid_power >= 0 else 'importing'})")
    print(f"  Voltage:     {d.grid_voltage} V  {d.grid_frequency} Hz")
    print(f"Temperature:   {d.inverter_temperature} °C")
    print()
    print(f"{'':15}{'today':>10}{'yesterday':>12}{'total':>10}")
    for key, label in ENERGY_LABELS.items():
        counter = d.energy[key]
        print(f"{label:15}{counter['today']:>8.1f} kWh{counter['yesterday']:>8.1f} kWh{counter['total']:>6} kWh")

    return data


def scan_for_logger():
    """Find the data logger on this computer's network (used when --ip isn't given)."""
    subnet = local_subnet()
    print(f"Scanning {subnet} for the data logger...", file=sys.stderr)
    loggers = find_inverters(subnet)
    if not loggers:
        sys.exit(f"No data logger found on {subnet}. Pass --ip if it's on a different network.")
    if len(loggers) > 1:
        print(f"Multiple data loggers found: {', '.join(loggers)}", file=sys.stderr)
    print(f"Using data logger at {loggers[0]} (pass --ip {loggers[0]} to skip the scan)\n", file=sys.stderr)
    return loggers[0]


if __name__ == "__main__":
    main()
