FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/solis.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY inverter.py store.py poller.py web.py index.html ./

# /data holds the database; compose mounts ./data from the host over it
RUN useradd --create-home --uid 1000 solis && mkdir /data && chown solis /data
USER solis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

# Exactly one worker: the background poller starts with the app, and more workers would mean
# several pollers reading the inverter and writing duplicate rows. Threads serve several browsers at once.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--access-logfile", "-", "web:app"]
