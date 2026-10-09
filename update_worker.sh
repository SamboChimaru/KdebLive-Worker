#!/bin/bash
set -e

echo "=========================================================="
echo "  🔄 24/7 Live Stream Worker Node - One-Click Updater"
echo "=========================================================="

CURRENT_DIR=$(pwd)
echo ">> [1/3] Pulling latest code from GitHub..."
git pull || true

echo ">> [2/3] Reloading systemd daemons..."
sudo systemctl daemon-reload

echo ">> [3/3] Restarting live-worker background service..."
sudo systemctl restart live-worker

echo ""
echo ">> Checking live-worker service status:"
sudo systemctl status live-worker --no-pager -n 10 || true

echo "=========================================================="
echo "  ✅ Live Worker Node updated and restarted successfully!"
echo "=========================================================="
