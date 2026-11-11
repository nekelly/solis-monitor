# Solis S6 inverter monitor

Reads live data from a Solis S6 hybrid inverter over Modbus TCP (port 502 on the data logger) and shows it either in the terminal or on a web page.

- `inverter.py`: the code that reads the inverter, shared by both of the below. It uses only the Python standard library.
- `solis_cli.py`: command-line tool. It needs nothing installed beyond Python.
- `web.py`: a Flask web dashboard built on the same reading code, designed to run in Docker. A background poller reads the inverter on a fixed schedule and saves every reading to a SQLite database.

## How the web app works

```
 Browser ── GET /api/data (just after each poll) ──► web.py ──► latest reading (in memory)
                                                       │
                                     poller.py thread, every POLL_SECONDS
                                                       │
              inverter.read_data() ──Modbus TCP──► data logger ──► inverter
                                                       │
                                     store.py ──► SQLite: data/solis.db (one row per poll)
```

- **Polling:** the poller runs inside the web app from the moment it starts, whether or not anyone has the page open. It reads the inverter on whole multiples of `POLL_SECONDS` (with 60 s, every minute on :00), so readings line up neatly for graphs.
- **Storage:** each reading becomes one row in the `readings` table, timestamped with the server's clock. Readings older than `RETENTION_DAYS` are deleted once a day.
- **The page never contacts the inverter.** It fetches the latest reading from the web app just after each poll, so it doesn't matter how many browsers are open.

## Files

```
solis/
  inverter.py            reads the inverter (Modbus TCP); used by the web app and the command-line tool
  solis_cli.py           command-line tool (not included in the Docker image)
  web.py                 Flask web app; starts the poller
  poller.py              background thread that reads the inverter every POLL_SECONDS
  store.py               SQLite storage for the readings
  index.html             the dashboard page
  requirements.txt       Python packages for the web app (Flask, gunicorn)
  Dockerfile             container image definition
  compose.yaml           Docker Compose deployment, including all settings
  .dockerignore          keeps caches, CSV logs, databases and virtual environments out of the image
  README.md              this file
  UPDATING.md            how to update the server to new code
  data/                  created by you on the server; holds solis.db and logger_address.json (see Deploying)
```

## Command-line tool

Requires Python 3.8 or newer. No packages are needed.

```
python solis_cli.py [--ip IP] [--loop N] [--csv FILE] [--summary] [--version]
```

| Option | What it does |
|---|---|
| *(none)* | Scans this computer's network for the data logger (a few seconds), prints the address it finds, and reads the inverter once |
| `--ip IP` | Uses this data logger address, with no scan |
| `--loop N` | Reads every N seconds until Ctrl+C. In a terminal the screen redraws each time and shows a countdown to the next update. Read errors are reported and the loop carries on |
| `--csv FILE` | Adds a row to FILE on each reading: timestamp, PV1, PV2, total solar, battery and grid power (W). The column names are written when the file is new |
| `--summary` | Shows daily PV1/PV2 energy (kWh) worked out from the `--csv` file, then exits. Needs `--csv`, and can't be combined with `--ip` or `--loop` |
| `--version` | Prints the version |

Examples:

```
python solis_cli.py --loop 30 --csv solar.csv     # log every 30 s
python solis_cli.py --csv solar.csv --summary     # daily per-string energy from the log
```

Notes:

- **Signs:** battery power is positive when charging and negative when discharging. Grid power is positive when exporting and negative when importing.
- **Summary accuracy:** `--summary` estimates energy from the logged power readings. Readings more than 10 minutes apart are treated as a gap and not counted (`MAX_GAP_SECONDS` in `solis_cli.py`). The *Logged* column shows how many hours each day's figures cover.
- **CSV columns:** if an existing CSV file has different columns (for example one from an older version), new rows aren't added to it. Start a new file instead.
- **Excel:** CSV files saved by Excel as "CSV UTF-8" are read correctly.

## Web dashboard

The page shows:

- **Power now:** solar, house load, battery (with a charge bar) and grid (Exporting/Importing/Idle).
- **Inverter:** each string's power, voltage, current and estimated energy so far today, plus AC output and temperature.
- **Energy:** today, yesterday and total figures.

The page updates itself about 3 seconds after each poll. The line under the title shows when the reading on screen was taken, counts down to the next update, and gives the polling interval, for example "Reading from 18:46:00 · update in 42 s · polled every 3 min". It follows the device's light or dark setting. If the latest poll failed, a red warning appears and the last good reading stays on screen, greyed out. A separate warning appears if the page can't reach the web app at all.

**Graphs:** click any box with a small graph icon in its bottom-right corner to open a graph over time:
- **Solar, House load, PV1 string and PV2 string:** power.
- **Battery:** charge level (%).
- **Grid:** grid power. Exports are above the zero line and imports below it, and the summary gives the largest export and the largest import.

**String energy today:** the inverter doesn't count energy per string, so the PV1 and PV2 boxes show an estimate of the energy since midnight, for example `330.9 V · 3.5 A · 4.2 kWh`.
- **How it's worked out:** the server adds up that string's stored power readings since midnight, recalculated after each poll.
- **Midnight:** taken from the inverter's clock, the same one its own daily counters use, so PV1 + PV2 should come close to "Solar today" in the energy table.
- **What it can miss:** gaps in polling, such as the server being off, aren't counted, so the estimate can fall short on those days.

**Rise/fall arrows:** the graph boxes also show a small ▲ or ▼ beside the label when their graphed value has risen or fallen since the previous reading. Hover over the arrow to see by how much. Power changes under 10 W count as steady and show no arrow. The comparison uses the previous poll from the server, so the arrows appear as soon as the page opens.
- **Ranges:** choose 6 hours (the default), 12 hours, 24 hours or 7 days.
- **Reading values:** move the pointer over the line to see the time and value.
- **Missing readings:** periods with no readings show as gaps rather than a straight line.
- **Smoothing:** the number of points depends on the graph's width on screen, about one every 4 pixels. Readings are averaged to fit, so a phone shows fewer, smoother points than a desktop. Resizing the window a lot fetches the graph again at the new density.
- **Summary:** the line below the graph gives the peak, which always comes from the individual readings so it isn't lowered by averaging, and how much the points are averaged.
- **Live:** while open, the graph updates with each new reading.
- **Closing:** use ×, Esc, or click outside it.

To add a graph to another box, give it `class="chart-trigger"` and `data-field` (a database column), `data-title`, `data-unit` and `data-color` attributes. Two optional extras:
- **`data-decimals`:** a fixed number of decimal places.
- **`data-peak-label`:** a different word for "Peak" in the summary.
- **`data-low-label`:** also show the lowest reading in the summary, under this label.
- **`data-signed="true"`:** for values where above and below zero mean different things, like grid power. The summary then shows the peak only if it's above zero and the low only if it's below, both as amounts without the minus sign.

See the Solar, Battery and Grid boxes in `index.html` for examples.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PAGE_TITLE` | `Solis S6` | Title shown at the top of the page and in the browser tab |
| `SOLIS_HOST` | *(not set)* | IP address of the inverter's data logger. Optional: if it's missing, or the logger stops answering there, the poller scans for it (see below) |
| `SCAN_SUBNET` | *(not set)* | Network to scan for the logger, e.g. `192.168.1.0/24`. Optional: by default it scans the /24 network that `SOLIS_HOST`, or the last known address, is on. Only needed if neither is known, because inside Docker the container can't work out your home network by itself |
| `POLL_SECONDS` | `60` | How often to read the inverter and save a reading, in whole seconds (minimum 5) |
| `RETENTION_DAYS` | `365` | Readings older than this are deleted (checked once a day). `0` keeps everything |
| `DB_PATH` | `solis.db` | Database file. The container sets this to `/data/solis.db`, which compose maps to `./data` on the host |
| `PORT` | `8080` | Port for the development server (`python web.py` only; the container always listens on 8080) |

An invalid number is logged and the default is used instead.

### Finding the data logger

The poller looks for the logger itself when it needs to:
- **When it scans:** at start-up if no address is known, or after 3 failed polls in a row.
- **How:** it tries every address on the network on port 502 (about 2 seconds) and checks that whatever answers really is the inverter. The logger sometimes doesn't answer for 20 seconds or so, so an empty scan is retried twice.
- **How often:** at most one scan every 10 minutes, so an offline logger doesn't cause constant scanning.
- **When it finds the logger at a new address:** it switches to it, logs `poller: data logger found at 192.168.1.19 (was 192.168.1.11)`, and saves the address in `logger_address.json` next to the database. In Docker that's `./data/logger_address.json`.
- **After a restart:** the saved address is used instead of `SOLIS_HOST`, unless you've changed `SOLIS_HOST` since the scan. The file records which `SOLIS_HOST` it replaced, so a deliberate change always wins.
- **Making it permanent:** when you see a "found at" message, update `SOLIS_HOST` in `compose.yaml` to match. A DHCP reservation in your router stops the address changing in the first place.

### Endpoints

| Path | Returns |
|---|---|
| `/` | The dashboard page |
| `/api/data` | JSON: `reading` (the latest successful reading, or `null` before the first one), `previous` (the successful reading before it, for the arrows), `pv_energy_today` (estimated kWh since midnight for `pv1_power` and `pv2_power`, or `null`), `age_seconds` (how old it is), `error` (why the latest poll failed, or `null`), `seconds_until_next_poll`, `poll_seconds`, and `taken_at` (Unix time) |
| `/api/history?field=…&range=…` | Stored readings of one database column (e.g. `solar_power`) for `range` = `6h`, `12h`, `24h` or `7d`. Returns `points` (`[[unix_time, value], …]`), `bucket_seconds`, `peak` and `low` (`[unix_time, value]` of the single highest and lowest readings). Optional `points` (20–1,500, default 1,500): roughly how many points to return. The page sends about one per 4 pixels of graph width (`PIXELS_PER_POINT` in `index.html`). If the range has no more readings than that, every reading is returned. Otherwise readings are averaged into steps of a whole number of poll intervals, so each step holds the same number of readings. The `peak` and `low` values always come from the individual readings. An unknown field gives 404 and an unknown range 400 |
| `/healthz` | `ok`, or HTTP 503 if the polling thread has stopped. Used by the Docker health check; it doesn't contact the inverter |

### The database

SQLite file with one table, `readings`. Columns:
- `ts`: Unix time in seconds (UTC), the primary key.
- One column per value from the inverter: `solar_power`, `pv1_voltage`, `battery_soc`, `grid_power`, `inverter_temperature` and so on. Power is in W, voltages in V, current in A, and temperature in °C.
- Energy counters as `<name>_today` and `<name>_total`, for example `solar_today` in kWh. "Yesterday" isn't stored, because it's the previous day's last `_today` value.

If a new value is added to `read_data()` later, a matching column is added automatically.

At 60-second polling the file grows by roughly 100–150 MB a year. It's in WAL mode, so you'll also see `solis.db-wal` and `solis.db-shm` next to it; treat the three files as one.

## Local testing (virtual environment, no Docker)

This runs the web app with Flask's built-in development server. Run all commands from the `solis` folder. The `.venv` folder is excluded from Docker builds by `.dockerignore`.

### Windows (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:SOLIS_HOST = "192.168.1.11"    # optional; without it the app scans for the logger
$env:POLL_SECONDS = "10"            # optional; faster polling for testing
$env:PAGE_TITLE = "Home Solar"      # optional
.venv\Scripts\python.exe web.py
```

This creates `solis.db` in the `solis` folder, and `logger_address.json` too if the app has to scan for the logger. It's excluded from Docker builds, and you can delete it at any time.

Notes for Windows:

- **Activating the environment:** you can run `.venv\Scripts\Activate.ps1` and then just use `python` and `pip`. If PowerShell refuses with an "execution policy" error, keep calling `.venv\Scripts\python.exe` directly as above.
- **gunicorn:** it installs on Windows but doesn't run there. That's fine, because `python web.py` uses Flask's own server instead. gunicorn is only used inside the container.

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

SOLIS_HOST=192.168.1.11 POLL_SECONDS=10 PAGE_TITLE="Home Solar" .venv/bin/python web.py
```

On Linux you can also test with gunicorn, the same server the container uses. Keep it to one worker, because each worker would start its own poller:

```bash
.venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 web:app
```

### Checking it works

Open <http://localhost:8080> in a browser, or check the endpoints from the command line:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz -UseBasicParsing       # PowerShell
Invoke-WebRequest http://127.0.0.1:8080/api/data -UseBasicParsing
```

```bash
curl http://127.0.0.1:8080/healthz                                      # Linux / macOS
curl http://127.0.0.1:8080/api/data
```

To test the error display, start the app with `SOLIS_HOST` set to an address with no data logger, for example `127.0.0.1`. After the first poll, the page should show the red "Can't reach inverter" warning. After the third failed poll the app scans 127.0.0.0/24, finds nothing, and keeps showing the warning. To test the scan itself, use an unused address on your own network instead, such as `192.168.1.250`. After three failed polls the app should find the real logger and carry on. Delete `logger_address.json` afterwards.

To look inside the database, use any SQLite tool, or Python:

```powershell
.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('solis.db'); print(c.execute('SELECT COUNT(*), datetime(MAX(ts), ''unixepoch'', ''localtime'') FROM readings').fetchone())"
```

Stop the server with Ctrl+C. To remove the test environment, delete the `.venv` folder.

## Deploying with Docker

These steps deploy from a Windows PC to a Linux host (Debian/Ubuntu commands shown) that you can reach over SSH. Replace `user@server` with your own login and the host's name or IP. The host must be **on the same network as the data logger**.

### 1. Check Docker on the host

```bash
docker --version
docker compose version
```

If either command fails, install Docker Engine with the Compose plugin, for example with Docker's install script:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER        # lets you run docker without sudo; log out and back in afterwards
sudo systemctl enable --now docker   # start Docker now and on every boot
```

### 2. Check the host can reach the data logger

```bash
timeout 3 bash -c '</dev/tcp/192.168.1.11/502' && echo "logger reachable" || echo "NOT reachable"
```

If it says NOT reachable, fix that first. Check the logger's IP address and that the host is on the same network.

### 3. Copy the project from Windows

In PowerShell on the PC, pack the folder without `.venv` and copy it over (`tar` and `scp` are built into Windows 10):

```powershell
tar -czf $env:TEMP\solis.tgz --exclude=.venv --exclude=__pycache__ --exclude=data --exclude="*.db*" --exclude="logger_address.json*" -C C:\Noel\Apps solis
scp $env:TEMP\solis.tgz user@server:~
```

Then on the host:

```bash
tar -xzf ~/solis.tgz -C ~      # creates ~/solis
rm ~/solis.tgz
cd ~/solis
```

### 4. Check the settings

```bash
nano compose.yaml
```

Check `PAGE_TITLE`, `SOLIS_HOST`, `POLL_SECONDS` and `RETENTION_DAYS` (see [Configuration](#configuration-environment-variables)). Keep the values in quotes. If port 8080 is already used on the host, change the first number in `"8080:8080"`, for example to `"8090:8080"`.

### 5. Create the data folder

The database lives in `~/solis/data` on the host. The app runs as user ID 1000 inside the container, so the folder must be writable by that ID:

```bash
mkdir -p data
sudo chown 1000:1000 data
```

Without this step, Docker creates the folder owned by root and the app stops with a "Can't open the database" error in the logs. Do it once; later deploys keep the folder and its data.

### 6. Build and start

```bash
docker compose up -d --build
```

The first build takes a minute or two while it downloads the Python image and installs Flask and gunicorn.

### 7. Check it's working

```bash
docker compose ps                              # STATUS should show "healthy" after about 30 seconds
docker compose logs -f                         # "poller: reading ... every 60 s", then a line per page request (Ctrl+C to stop watching)
curl -s localhost:8080/api/data | head -c 200  # should print inverter JSON
ls -l data                                     # solis.db (plus -wal and -shm files) should appear after the first poll
```

Then open `http://<server-ip>:8080` in a browser.

### Afterwards

Run these in `~/solis` on the host:

| Task | Command |
|---|---|
| Change a setting | edit `compose.yaml`, then `docker compose up -d` (no rebuild needed) |
| Deploy new code | see `UPDATING.md` for the full steps |
| View logs | `docker compose logs -f` (capped at 3 × 10 MB; older lines are discarded automatically) |
| Check status and health | `docker compose ps` |
| Stop / start | `docker compose stop` / `docker compose start` |
| Back up the database | `sqlite3 data/solis.db ".backup solis-backup.db"` (safe while running; install with `sudo apt install sqlite3`) |
| Remove completely | `docker compose down`, then `docker image rm solis-web`; delete `data/` too if you don't want the history |

**Deploying new code:** the tar command in step 3 doesn't include a `data` folder, so unpacking over `~/solis` leaves your database alone.

**After a reboot:** the container starts again automatically, because Docker is enabled at boot (step 1) and `compose.yaml` has `restart: unless-stopped`. If you stop it with `docker compose stop`, it stays stopped until you start it again.

**Firewall:** ports published by Docker usually bypass `ufw`, so the page is reachable from your network even if ufw is on. That's fine at home. Don't forward the port on your router, because the page has no login.

**Troubleshooting:**
- **Red "Can't reach inverter" warning on the page:** the container can't reach the logger. Run the step 2 check on the host, and check `SOLIS_HOST` in `compose.yaml`. After 3 failures the poller scans for the logger by itself; `docker compose logs | grep poller` shows whether it found it.
- **The container keeps restarting, or `ps` shows "unhealthy":** run `docker compose logs` to see the error. "Can't open the database" means the `data` folder is missing or not owned by uid 1000 (step 5).

**How the container runs:** gunicorn runs as a non-root user (uid 1000), with one worker and four threads. It must stay at one worker, because the poller starts with the app and each extra worker would start another poller.

## Housekeeping

| What | How it's handled |
|---|---|
| Old readings | Deleted automatically once a day when older than `RETENTION_DAYS` |
| Database size | Levels off once retention starts deleting: deleted rows' space is reused, though the file never shrinks. After lowering `RETENTION_DAYS` a lot, you can compact it with `sqlite3 data/solis.db "VACUUM"`, ideally with the container stopped |
| Container logs | Rotated by Docker at 10 MB, keeping 3 files (the `logging` section of `compose.yaml`) |
| Backups | Not automatic. Use the backup command in the table above, by hand or from cron |

## Things to know

- **Fixed IP address:** the logger's address can change if your router assigns it a new one; it has been seen at both 192.168.1.11 and 192.168.1.19. The web app finds it again by itself (see [Finding the data logger](#finding-the-data-logger)), but a DHCP reservation in the router, so it always gets the same address, avoids the gap in readings while that happens.
- **No login:** the web page has no password. Keep it on your home network and don't expose the port to the internet.
- **Version:** the version number is `VERSION` near the top of `inverter.py`. The command-line tool's `--version` and the web page's footer both use it.
- **Suspicious counter:** the lifetime grid-export counter (about 6,660 kWh) is higher than lifetime solar production (about 2,360 kWh), which shouldn't be possible. It may have been counting before this inverter was installed, or mean something different on this firmware. Daily figures look consistent. Worth checking against the Solis app.
