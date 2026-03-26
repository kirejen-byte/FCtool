# FCTool Headless - VPS Deployment Guide

## What runs on the VPS
- zKillboard WebSocket monitor (real-time kill stream)
- Discord webhook notifications (fight alerts, capital alerts)
- Route calculations from your staging system
- Runs 24/7, auto-restarts on crash or reboot

## What stays local (your PC)
- Full GUI with X-up counter, jump range, WH routes
- Chat log scanning (needs access to EVE log files)
- Screenshot uploads, role tracker

---

## Step 1: Get a VPS

Cheapest options that work fine for this:
- **Oracle Cloud** - Free tier (always free, 1GB RAM ARM) - best if you want $0/mo
- **Hetzner** - ~$4/mo (2GB RAM, EU/US datacenters)
- **DigitalOcean** - $4/mo (512MB RAM)
- **Vultr** - $2.50/mo (512MB RAM) - cheapest paid option
- **AWS Lightsail** - $3.50/mo (512MB RAM)

Any Linux VPS with Python 3.10+ works. **Ubuntu 22.04+** recommended.
FCTool uses ~30MB RAM so even the smallest tier is fine.

---

## Step 2: SSH into your VPS

When you create the VPS, you'll get an IP address and either a password or SSH key.

```bash
ssh root@YOUR_VPS_IP
```

If you set up with an SSH key:
```bash
ssh -i ~/.ssh/your_key root@YOUR_VPS_IP
```

First time connecting, type `yes` when asked about the fingerprint.

---

## Step 3: Upload files

Open a **new terminal on your PC** (not the SSH session). Navigate to your FCTool folder and upload:

```bash
# From your PC — run this in PowerShell or Git Bash
cd "C:\Users\OWNER\OneDrive\Documents\Projects\FCTool"

scp fc_headless.py zkill_monitor.py discord_notify.py jump_range.py rate_limiter.py config.json root@YOUR_VPS_IP:~/fctool/
scp deploy/setup.sh root@YOUR_VPS_IP:~/fctool/
```

Replace `YOUR_VPS_IP` with your VPS IP (e.g., `143.198.100.50`).

If you don't have `scp` on Windows, you can use **WinSCP** (free GUI tool) or **FileZilla** with SFTP.

---

## Step 4: Run the setup script

Back in your **SSH session**:

```bash
cd ~/fctool
bash setup.sh
```

This will:
1. Install Python 3 + pip if needed
2. Create a virtual environment with dependencies
3. Create a systemd service that auto-starts on boot
4. Start the monitor immediately

---

## Step 5: Verify it works

```bash
# Check service status (should say "active (running)")
sudo systemctl status fctool

# Watch live logs
journalctl -u fctool -f
```

You should see output like:
```
FCTool Headless starting
  Regions: 8 (Fountain, Syndicate, Outer Ring, Pure Blind, Cloud Ring, Delve, Fade, Aridia)
  Alliances: 12 (The Initiative., Fraternity., ...)
  Min pilots: 25 (any if capitals)
  Staging: C-N4OD
  Alert window: 300s
Monitoring active.
```

And a "FCTool Monitor: ONLINE" embed should appear in your Discord channel.

Press `Ctrl+C` to stop watching logs (the service keeps running).

---

## Day-to-day management

```bash
sudo systemctl status fctool      # Check status
sudo systemctl restart fctool     # Restart (after config changes)
sudo systemctl stop fctool        # Stop
sudo systemctl start fctool       # Start
journalctl -u fctool -f           # Live logs
journalctl -u fctool --since today  # Today's logs
journalctl -u fctool --since "1 hour ago"  # Last hour
```

## Updating config

Edit directly on the VPS:
```bash
nano ~/fctool/config.json
sudo systemctl restart fctool
```

Or re-upload from your PC:
```bash
scp config.json root@YOUR_VPS_IP:~/fctool/
ssh root@YOUR_VPS_IP "sudo systemctl restart fctool"
```

## Updating code

When you make changes to the tool:
```bash
scp fc_headless.py zkill_monitor.py discord_notify.py jump_range.py rate_limiter.py root@YOUR_VPS_IP:~/fctool/
ssh root@YOUR_VPS_IP "sudo systemctl restart fctool"
```

---

## Security notes

- The `config.json` on the VPS contains your Discord webhook URL. Keep it private.
- The VPS only makes outbound connections (zKill WebSocket, ESI API, Discord). No ports need to be opened.
- Consider creating a non-root user: `adduser fctool && su - fctool` and run everything as that user.

## Troubleshooting

**Service won't start:**
```bash
journalctl -u fctool -n 50    # Check last 50 log lines for errors
```

**WebSocket disconnects:**
The service auto-reconnects after 5 seconds. If Cloudflare blocks it, check the User-Agent header in zkill_monitor.py.

**No Discord messages:**
Verify the webhook URL is correct. Test manually:
```bash
curl -X POST "YOUR_WEBHOOK_URL" -H "Content-Type: application/json" -d '{"content":"test"}'
```
