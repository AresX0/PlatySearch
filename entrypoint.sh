#!/bin/sh
set -e

# ── Determine DB locations ──────────────────────────────────────
# Always use /app/data (matches the pydantic default) so the app
# works even if the PLATY_DB_PATH env-var is not picked up.
DB_DIR="/app/data"
DB_PATH="$DB_DIR/platysearch.db"
mkdir -p "$DB_DIR"

# Azure persistent storage — the ONLY authoritative copy of the DB.
PERSIST_DIR="/home/data"
have_persist=0
if mkdir -p "$PERSIST_DIR" 2>/dev/null; then
    have_persist=1
fi

# ── Restore from persistent storage (Azure /home/data) ──────────
# This is the primary DB source across container restarts / redeploys.
if [ "$have_persist" = "1" ] && [ -f "$PERSIST_DIR/platysearch.db" ] && [ -s "$PERSIST_DIR/platysearch.db" ]; then
    echo "Restoring DB from persistent storage ($PERSIST_DIR) ..."
    cp "$PERSIST_DIR/platysearch.db" "$DB_PATH"
    echo "Live DB restored ($(du -h "$DB_PATH" | cut -f1))."
fi

# ── Seed DB — ONLY used for brand-new installs (no DB anywhere) ─
# The seed DB is NEVER used to overwrite a live/persistent DB.
# To force a seed replacement, set env var PLATY_FORCE_SEED=1.
if [ ! -f "$DB_PATH" ] || [ "${PLATY_FORCE_SEED:-0}" = "1" ]; then
    if [ -f "/app/seed/platysearch.db.gz" ]; then
        echo "No existing DB found — decompressing seed database to $DB_PATH ..."
        gunzip -c /app/seed/platysearch.db.gz > "$DB_PATH"
        echo "Seed database ready ($(du -h "$DB_PATH" | cut -f1))."
        # Persist immediately so subsequent restarts pick it up.
        if [ "$have_persist" = "1" ]; then
            cp "$DB_PATH" "$PERSIST_DIR/platysearch.db" 2>/dev/null || true
        fi
        # Clear the force flag file (env var is one-shot).
    fi
fi

export PLATY_DB_PATH="$DB_PATH"

exec uvicorn platysearch.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
