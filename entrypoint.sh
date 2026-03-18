#!/bin/sh
set -e

# ── Determine DB locations ──────────────────────────────────────
# Always use /app/data (matches the pydantic default) so the app
# works even if the PLATY_DB_PATH env-var is not picked up.
DB_DIR="/app/data"
DB_PATH="$DB_DIR/platysearch.db"
mkdir -p "$DB_DIR"

# Also try to keep a copy on Azure persistent storage (/home/data).
PERSIST_DIR="/home/data"
if mkdir -p "$PERSIST_DIR" 2>/dev/null; then
    # If a good copy already exists on persistent storage, reuse it.
    if [ -f "$PERSIST_DIR/platysearch.db" ] && [ -s "$PERSIST_DIR/platysearch.db" ]; then
        cp "$PERSIST_DIR/platysearch.db" "$DB_PATH" 2>/dev/null || true
    fi
fi

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
    echo "Decompressing seed database to $DB_PATH ..."
    gunzip -c /app/seed/platysearch.db.gz > "$DB_PATH"
    [ -f "$SEED_VER_FILE" ] && cp "$SEED_VER_FILE" "$LIVE_VER_FILE"
    echo "Seed database ready ($(du -h "$DB_PATH" | cut -f1))."
    # Persist a copy so the next container start can skip decompression.
    if [ -d "$PERSIST_DIR" ]; then
        cp "$DB_PATH" "$PERSIST_DIR/platysearch.db" 2>/dev/null || true
        [ -f "$SEED_VER_FILE" ] && cp "$SEED_VER_FILE" "$PERSIST_DIR/.seed_version" 2>/dev/null || true
    fi
fi

exec uvicorn platysearch.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
