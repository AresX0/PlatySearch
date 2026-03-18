#!/bin/sh
set -e

# ── Determine DB locations ──────────────────────────────────────
# Azure persistent storage is at /home; /app/data is the fallback.
DB_DIR="/home/data"
DB_PATH="$DB_DIR/platysearch.db"

# If /home is not writable (local Docker), use /app/data instead.
if ! mkdir -p "$DB_DIR" 2>/dev/null; then
    DB_DIR="/app/data"
    DB_PATH="$DB_DIR/platysearch.db"
    mkdir -p "$DB_DIR"
fi

# Export so pydantic-settings picks it up regardless of Dockerfile ENV.
export PLATY_DB_PATH="$DB_PATH"

# ── Replace the DB when a new seed version ships ────────────────
SEED_VER_FILE="/app/seed/VERSION"
LIVE_VER_FILE="$DB_DIR/.seed_version"

need_seed=0
if [ ! -f "$DB_PATH" ]; then
    need_seed=1
elif [ -f "$SEED_VER_FILE" ]; then
    cur=$(cat "$LIVE_VER_FILE" 2>/dev/null || echo "")
    new=$(cat "$SEED_VER_FILE")
    if [ "$cur" != "$new" ]; then
        need_seed=1
    fi
fi

if [ "$need_seed" = "1" ]; then
    echo "Decompressing seed database to $DB_PATH …"
    gunzip -c /app/seed/platysearch.db.gz > "$DB_PATH"
    [ -f "$SEED_VER_FILE" ] && cp "$SEED_VER_FILE" "$LIVE_VER_FILE"
    echo "Seed database ready ($(du -h "$DB_PATH" | cut -f1))."
fi

exec uvicorn platysearch.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
