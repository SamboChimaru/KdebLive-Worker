#!/bin/bash
set -e

echo "=========================================================="
echo "  🚀 24/7 Live Stream Bot - AWS Worker Node Setup (Phase 2)"
echo "=========================================================="

# 1. Check root or sudo
if [ "$(id -u)" != "0" ]; then
    echo "⚠️ This script requires sudo / root privileges."
    echo "Please run: sudo bash start_worker.sh"
    exit 1
fi

CURRENT_DIR=$(pwd)
TARGET_USER=${SUDO_USER:-$(whoami)}

# 2. Add 2GB Swap Memory (Crucial for 1 GB RAM / 2 GB RAM AWS Lightsail stability)
if [ ! -f /swapfile ]; then
    echo ">> [1/5] Configuring 2GB Swap memory for FFmpeg transcoding stability..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo ">> Swap configured."
else
    echo ">> [1/5] Swap file already exists, skipping."
fi

# 3. Install FFmpeg & Python3
echo ">> [2/5] Installing FFmpeg and Python3 prerequisites..."
apt-get update -o Acquire::ForceIPv4=true -y
apt-get install -y python3 python3-pip python3-venv ffmpeg curl ufw

# 4. Open Port 8000 on firewall if UFW is active
if ufw status | grep -q "Status: active"; then
    echo ">> [3/5] Opening Port 8000 for Controller connections..."
    ufw allow 8000/tcp
fi

# 5. Setup Python virtual environment
echo ">> [4/5] Setting up Python virtual environment..."
cd "$CURRENT_DIR"
if [ ! -d "venv" ]; then
    sudo -u "$TARGET_USER" python3 -m venv venv
fi
sudo -u "$TARGET_USER" ./venv/bin/pip install --upgrade pip
sudo -u "$TARGET_USER" ./venv/bin/pip install -r requirements.txt

# Create necessary directories
mkdir -p data/downloads data/logs
chown -R "$TARGET_USER:$TARGET_USER" .

# 6. Configure Systemd Service for worker.py
echo ">> [5/5] Configuring 24/7 background systemd service (live-worker.service)..."
SERVICE_PATH="/etc/systemd/system/live-worker.service"

cat << EOF > "$SERVICE_PATH"
[Unit]
Description=24/7 Live Stream Worker Node Daemon (Phase 2)
After=network.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$CURRENT_DIR
EnvironmentFile=-$CURRENT_DIR/.env
ExecStart=$CURRENT_DIR/venv/bin/python $CURRENT_DIR/worker.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable live-worker
systemctl restart live-worker

echo "=========================================================="
echo "  ✅ AWS Worker Node is now running on Port 8000!"
echo "=========================================================="
echo "• Worker Port:      8000"
echo "• Health check:     curl http://127.0.0.1:8000/health"
echo "• Check status:     sudo systemctl status live-worker"
echo "• View live logs:   sudo journalctl -u live-worker -f"
echo "• Restart worker:   sudo systemctl restart live-worker"
echo "• Stop worker:      sudo systemctl stop live-worker"
echo "=========================================================="
