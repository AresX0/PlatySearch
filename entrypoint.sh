#!/bin/sh
set -e

# Ensure persistent data directory exists.
mkdir -p /home/data

# Replace the DB when a new seed version ships.
SEED_VER_FILE="/app/seed/VERSION"
LIVE_VER_FILE="/home/data/.seed_version"

need_seed=0
if [ ! -f /home/data/platysearch.db ]; then
    need_seed=1
elif [ -f "$SEED_VER_FILE" ]; then
    cur=$(cat "$LIVE_VER_FILE" 2>/dev/null || echo "")
    new=$(cat "$SEED_VER_FILE")
    if [ "$cur" != "$new" ]; then
        need_seed=1
    fi
fi

if [ "$need_seed" = "1" ]; then
    echo "Decompressing seed database…"
    gunzip -c /app/seed/platysearch.db.gz > /home/data/platysearch.db
    [ -f "$SEED_VER_FILE" ] && cp "$SEED_VER_FILE" "$LIVE_VER_FILE"
    echo "Seed database ready."
fi

exec uvicorn platysearch.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
