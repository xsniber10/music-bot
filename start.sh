#!/bin/sh
set -e

echo "[potprovider] Starting bgutil PO Token provider on port 4416..."
node /opt/bgutil-pot-provider/build/main.js &

exec python bot.py
