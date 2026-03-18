#!/bin/sh
set -e

# Ensure persistent data directory exists.
mkdir -p /home/data

if [ ! -f /home/data/platysearch.db ]; then
    echo "No existing DB found — decompressing seed database…"
    gunzip -c /app/seed/platysearch.db.gz > /home/data/platysearch.db
    echo "Seed database ready."
fi

exec uvicorn platysearch.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
