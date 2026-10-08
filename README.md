# 🚀 24/7 Live Stream Worker Node Daemon (Phase 2)

Lightweight, high-performance streaming agent designed to run 24/7 on AWS Lightsail, EC2, or any Linux VPS. It executes hardware-efficient FFmpeg transcoding (1080p CBR, 720p, stream-copy), handles video downloading directly into local cloud storage, and streams continuously to YouTube, Facebook Live, and custom RTMP destinations.

This repository is **completely standalone, open-source, and contains zero bot tokens or sensitive secrets**.

---

## ⚡ Fast 1-Minute Deployment on AWS Lightsail / EC2 / VPS

### Step 1: Open Port 8000 on Firewall
- In **AWS Lightsail Console** > Your Instance > **Networking** tab > **IPv4 Firewall**:
  - Add Rule: **Custom TCP**, Port: `8000`.

### Step 2: Run the Installer (Ubuntu / Debian)
SSH into your AWS instance and run:

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/YOUR_PUBLIC_WORKER_REPO.git ~/live-worker
cd ~/live-worker
sudo bash start_worker.sh
```

**What the setup script automatically configures:**
- 🛡️ **2 GB Swap memory**: Prevents memory spikes and OOM crashes during FFmpeg encoding.
- 🎬 **FFmpeg & Python 3**: Latest media transcoders and virtual environment.
- ⚡ **Minimal dependencies**: Installs lightweight async daemon (`aiohttp`, `psutil`, `yt-dlp`).
- 🔄 **Systemd background service (`live-worker.service`)**: Runs 24/7, auto-starts on server boot, and auto-restarts if interrupted.

---

## 🔧 Optional: Custom API Key

By default, the worker uses the secret key specified in `.env`. To set your own custom secret key:

```bash
cd ~/live-worker
nano .env
```
Add:
```ini
WORKER_PORT=8000
WORKER_API_KEY=your_custom_secret_key_here
```
Then restart the service:
```bash
sudo systemctl restart live-worker
```

---

## 📡 Useful Service Commands

| Action | Command |
| :--- | :--- |
| **Check service status** | `sudo systemctl status live-worker` |
| **View live logs** | `sudo journalctl -u live-worker -f` |
| **Restart worker** | `sudo systemctl restart live-worker` |
| **Stop worker** | `sudo systemctl stop live-worker` |
| **Test health locally** | `curl http://127.0.0.1:8000/health` |

---

## 🤖 Connecting to Your Telegram Master Controller

In your private Telegram bot chat, register this AWS worker node using:

```text
/addserver <server_id> "<server_name>" <AWS_PUBLIC_IP> 8000 <WORKER_API_KEY> <region>
```

**Example:**
```text
/addserver srv-1 "AWS Singapore" 13.250.1.2 8000 default_secret_key ap-southeast-1
```

Once registered:
- Tap **🖥 Server Health** or send `/server` to view live CPU, RAM, disk, and active stream metrics directly from your AWS instance!
- Tap **🔴 Live YouTube** or **🔵 Live Facebook** to broadcast 24/7.
