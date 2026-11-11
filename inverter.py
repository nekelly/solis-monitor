#!/usr/bin/env python3

# Author: Noel Kelly

"""Reads a Solis S6 inverter over Modbus TCP via its data logger.

Shared by the web app (poller.py, web.py) and the command-line tool (solis_cli.py).
Standard library only.
"""

import ipaddress
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.2"
PORT = 502
UNIT = 1
MAX_SCAN_HOSTS = 1024   # refuse to scan networks bigger than a /22
# How long each address gets to accept a connection during a scan. A Wi-Fi data logger waking from
# power saving can take well over half a second (0.5 s missed it in 2 scans out of 5)
SCAN_CONNECT_TIMEOUT = 2.0

ENERGY_LABELS = {
    "solar": "Solar",
    "house_load": "House load",
    "battery_in": "Battery in",
    "battery_out": "Battery out",
    "grid_import": "Grid import",
    "grid_export": "Grid export",
}


def read_data(host):
    """Read the inverter and return all values as a dict."""
    regs = read_input_registers(host, 33000, 100) + read_input_registers(host, 33100, 100)
    u16 = lambda r: regs[r - 33000]
    s16 = lambda r: u16(r) - 0x10000 if u16(r) & 0x8000 else u16(r)
    s32 = lambda r: struct.unpack(">i", struct.pack(">HH", u16(r), u16(r + 1)))[0]

    year, month, day = u16(33022), u16(33023), u16(33024)
    hour, minute, second = u16(33025), u16(33026), u16(33027)

    solar_power = s32(33057)
    pv1_voltage = u16(33049) / 10
    pv1_current = u16(33050) / 10
    pv2_voltage = u16(33051) / 10
    pv2_current = u16(33052) / 10
    pv1_power = round(pv1_voltage * pv1_current)
    pv2_power = round(pv2_voltage * pv2_current)
    inverter_ac_power = s32(33079)
    house_load_power = u16(33147)

    battery_discharging = bool(u16(33135))
    battery_power = s32(33149) * (-1 if battery_discharging else 1)
    battery_soc = u16(33139)
    battery_health = u16(33140)
    battery_voltage = u16(33133) / 10

    grid_power = s32(33130)
    grid_voltage = u16(33073) / 10
    grid_frequency = u16(33094) / 100
    inverter_temperature = s16(33093) / 10

    # Energy counters: today/yesterday registers are in 0.1 kWh, totals in whole kWh
    energy_counter = lambda today_reg, yesterday_reg, total_reg: {
        "today": u16(today_reg) / 10,
        "yesterday": u16(yesterday_reg) / 10,
        "total": s32(total_reg),
    }

    return {
        "inverter_time": f"20{year:02}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}",
        "solar_power": solar_power,
        "pv1_voltage": pv1_voltage,
        "pv1_current": pv1_current,
        "pv1_power": pv1_power,
        "pv2_voltage": pv2_voltage,
        "pv2_current": pv2_current,
        "pv2_power": pv2_power,
        "inverter_ac_power": inverter_ac_power,
        "house_load_power": house_load_power,
        "battery_discharging": battery_discharging,
        "battery_power": battery_power,
        "battery_soc": battery_soc,
        "battery_health": battery_health,
        "battery_voltage": battery_voltage,
        "grid_power": grid_power,
        "grid_voltage": grid_voltage,
        "grid_frequency": grid_frequency,
        "inverter_temperature": inverter_temperature,
        "energy": {
            "solar": energy_counter(33035, 33036, 33029),
            "house_load": energy_counter(33179, 33180, 33177),
            "battery_in": energy_counter(33163, 33164, 33161),
            "battery_out": energy_counter(33167, 33168, 33165),
            "grid_import": energy_counter(33171, 33172, 33169),
            "grid_export": energy_counter(33175, 33176, 33173),
        },
    }


def find_inverters(subnet, attempts=3, retry_delay=5):
    """Return the addresses on subnet (e.g. "192.168.1.0/24") that answer as a Solis data logger.

    Every address is tried on port 502 at once (about 2 seconds), then each one that's open is
    checked with a register read, so other Modbus devices aren't mistaken for the logger. The logger
    sometimes stops answering for 20 seconds or so, so an empty scan is retried a couple of times.
    """
    network = ipaddress.ip_network(subnet, strict=False)
    if network.num_addresses > MAX_SCAN_HOSTS:
        raise ValueError(f"{subnet} is too big to scan (more than {MAX_SCAN_HOSTS} addresses)")
    candidates = [str(address) for address in network.hosts()]
    for attempt in range(attempts):
        if attempt:
            time.sleep(retry_delay)
        # One thread per address (up to 256), so a /24 takes a single round of SCAN_CONNECT_TIMEOUT
        with ThreadPoolExecutor(max_workers=min(256, max(1, len(candidates)))) as pool:
            open_hosts = [h for h, is_open in zip(candidates, pool.map(port_open, candidates)) if is_open]
        found = [h for h in open_hosts if is_inverter(h)]
        if found:
            return found
    return []


def subnet_of(host):
    """The /24 network an address is on, e.g. 192.168.1.11 -> "192.168.1.0/24"."""
    return str(ipaddress.ip_network(f"{host}/24", strict=False))


def local_subnet():
    """The /24 network this computer uses to reach the internet. Inside Docker this is Docker's own
    network, not the home network, so the web app prefers subnet_of() a known address."""
    # Connecting a UDP socket sends no packets; it just picks the outgoing interface
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return subnet_of(s.getsockname()[0])


def port_open(host):
    try:
        with socket.create_connection((host, PORT), timeout=SCAN_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def is_inverter(host):
    # Something listening on port 502 might not be the inverter, so check it answers a register read
    try:
        read_input_registers(host, 33000, 1)
        return True
    except (OSError, RuntimeError):
        return False


def read_input_registers(host, register, count):
    with socket.create_connection((host, PORT), timeout=3) as s:
        s.sendall(struct.pack(">HHHBBHH", 1, 0, 6, UNIT, 4, register, count))
        # MBAP header: bytes 4-5 give the number of bytes that follow byte 5
        resp = b""
        while len(resp) < 6 or len(resp) < 6 + struct.unpack(">H", resp[4:6])[0]:
            chunk = s.recv(512)
            if not chunk:
                raise RuntimeError(f"connection closed after {len(resp)} bytes reading {register}")
            resp += chunk

    if len(resp) < 9:
        raise RuntimeError(f"short response ({len(resp)} bytes) reading {register}")
    tid, proto, _, _, func, byte_count = struct.unpack(">HHHBBB", resp[:9])
    if tid != 1 or proto != 0:
        raise RuntimeError(f"unexpected transaction/protocol id ({tid}/{proto}) reading {register}")
    if func == 0x84:
        raise RuntimeError(f"modbus exception {byte_count} reading {register}")
    if func != 4:
        raise RuntimeError(f"unexpected function code {func} reading {register}")
    if byte_count != count * 2 or len(resp) < 9 + byte_count:
        raise RuntimeError(f"expected {count * 2} data bytes, got {byte_count} reading {register}")
    return list(struct.unpack(f">{count}H", resp[9 : 9 + byte_count]))
