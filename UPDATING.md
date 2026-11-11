# Updating the server to new code

How to update the Docker container on the Linux server after changing the code on the Windows PC. For a first-time install, follow **Deploying with Docker** in `README.md` instead.

Replace `user@server` with your own login and the server's name or IP address.

## 1. On the Windows PC: pack and copy the code

```powershell
tar -czf $env:TEMP\solis.tgz --exclude=.venv --exclude=__pycache__ --exclude=data --exclude="*.db*" --exclude="logger_address.json*" -C C:\Noel\Apps solis
scp $env:TEMP\solis.tgz user@server:~
```

The excludes leave out the virtual environment, Python caches and any local test database. Nothing from the PC can overwrite the data on the server.

## 2. On the server: unpack over the existing folder

**Settings:** unpacking replaces the server's `compose.yaml` with the PC's copy. If you've changed settings directly on the server, save that copy first and compare afterwards:

```bash
cp ~/solis/compose.yaml ~/compose.yaml.server   # only needed if you edited it on the server
tar -xzf ~/solis.tgz -C ~
rm ~/solis.tgz
cd ~/solis
```

Check the settings with `nano compose.yaml`. The current ones are `PAGE_TITLE`, `SOLIS_HOST`, `POLL_SECONDS` and `RETENTION_DAYS`. Versions before the database was added used `REFRESH_SECONDS` and `ENABLED`, which no longer exist.

## 3. First time only: create the data folder

Only needed the first time you update to a version with the database (the background poller):

```bash
mkdir -p data
sudo chown 1000:1000 data
```

On later updates, skip this step. The folder and the database in it are never touched by an update.

## 4. Rebuild and restart

```bash
docker compose up -d --build
```

This builds the new image and replaces the running container, applying the new code and any changes to `compose.yaml`. The page is unavailable for a few seconds.

## 5. Check it's working

```bash
docker compose ps                    # STATUS should become "healthy" within about 30 seconds
docker compose logs --tail 20        # should include "poller: reading 192.168.1.11 every 60 s"
ls -l data                           # solis.db, solis.db-wal and solis.db-shm (after the first poll)
curl -s localhost:8080/api/data | head -c 300
```

After a couple of minutes, check that history is building up:

```bash
curl -s "localhost:8080/api/history?field=solar_power&range=24h" | head -c 300
```

Then reload the page in your browser. If it still looks old, use Ctrl+F5 to force a full reload.

## 6. Optional: remove the old image

```bash
docker image prune -f
```

Each rebuild leaves the previous image behind, unused. This deletes those leftovers.

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| Container keeps restarting; logs say "Can't open the database" | The `data` folder is missing or not owned by uid 1000. Redo step 3, then `docker compose up -d` |
| Logs say "Can't read inverter" | The poller can't reach the data logger. The page and database still work, but no readings are saved. Check `SOLIS_HOST` in `compose.yaml`, and that the server can reach the logger (step 2 of **Deploying with Docker** in `README.md`) |
| Graphs say "No readings in this period yet" | Normal straight after the first update to the database version. History starts from then, and the graphs fill in as readings build up |
| Page looks unchanged | The browser is showing a cached copy. Use Ctrl+F5 |
| Need to go back to the previous version | Unpack the previous code (keep a copy of the old `solis.tgz` before updating), then `docker compose up -d --build`. The `data` folder isn't affected |
