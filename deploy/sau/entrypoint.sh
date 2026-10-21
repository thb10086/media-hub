#!/bin/sh
# /app/db is a named volume seeded from the image on first use; the table
# script is idempotent (CREATE TABLE IF NOT EXISTS), so it runs on every start.
set -eu

cd /app/db
python createTable.py >/dev/null

# Patch Douyin login QR detection for new page layout (idempotent).
cd /app
python /usr/local/share/sau-patches/patch-login.py 2>/dev/null || true
# Douyin posts carry their body text and a portrait cover (idempotent).
python /usr/local/share/sau-patches/patch-publish.py 2>/dev/null || true

exec python sau_backend.py "$@"
