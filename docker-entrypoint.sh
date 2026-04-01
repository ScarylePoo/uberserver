#!/bin/bash
set -e

# ── Wait for MariaDB to be ready ───────────────────────────────────────────────
echo "Waiting for MariaDB at ${DB_HOST:-db}:${DB_PORT:-3306}..."
until python3 -c "
import sys, time
try:
    import MySQLdb
    MySQLdb.connect(
        host='${DB_HOST:-db}',
        port=${DB_PORT:-3306},
        user='${DB_USER:-uberserver}',
        passwd='${DB_PASSWORD}',
        db='${DB_NAME:-uberserver}'
    )
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    echo "  MariaDB not ready yet, retrying in 2s..."
    sleep 2
done
echo "MariaDB is up."

# ── Build the SQL URL ──────────────────────────────────────────────────────────
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-uberserver}"
DB_NAME="${DB_NAME:-uberserver}"
SQLURL="mysql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?charset=utf8"

# ── Start uberserver ───────────────────────────────────────────────────────────
echo "Starting uberserver..."
exec python3 server.py \
    --port "${LOBBY_PORT:-8200}" \
    --natport "${NAT_PORT:-8201}" \
    --sqlurl "${SQLURL}" \
    ${EXTRA_ARGS:-}
