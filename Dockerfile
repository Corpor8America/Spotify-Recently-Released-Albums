FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# -w 1: exactly one worker process, so APScheduler's in-process cron job
# and the run_lock that serializes scans only ever exist once. Do not
# raise this without moving the scheduler + lock out of the app process
# (e.g. a separate worker container) first.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--timeout", "180", "app:app"]
