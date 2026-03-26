#!/bin/bash
# FCTool Headless - VPS Setup Script
# Run this on a fresh Ubuntu/Debian VPS
# Usage: bash setup.sh

set -e

echo "=== FCTool Headless Setup ==="

# Install Python if needed
if ! command -v python3 &> /dev/null; then
    echo "Installing Python 3..."
    sudo apt update
    sudo apt install -y python3 python3-pip python3-venv
fi

# Create app directory
APP_DIR="$HOME/fctool"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install requests websocket-client

# Check required files
REQUIRED_FILES="fc_headless.py zkill_monitor.py discord_notify.py jump_range.py rate_limiter.py config.json"
MISSING=""
for f in $REQUIRED_FILES; do
    if [ ! -f "$f" ]; then
        MISSING="$MISSING  - $f\n"
    fi
done

if [ -n "$MISSING" ]; then
    echo ""
    echo "ERROR: Missing files in $APP_DIR:"
    echo -e "$MISSING"
    echo "Upload them first with:"
    echo "  scp fc_headless.py zkill_monitor.py discord_notify.py jump_range.py rate_limiter.py config.json user@your-vps:~/fctool/"
    exit 1
fi

echo "Files found. Setting up systemd service..."

# Create systemd service
sudo tee /etc/systemd/system/fctool.service > /dev/null << EOF
[Unit]
Description=FCTool Headless - EVE Online zKill Monitor
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/fc_headless.py --config $APP_DIR/config.json
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable fctool
sudo systemctl start fctool

echo ""
echo "=== Setup Complete ==="
echo ""
echo "FCTool is now running as a systemd service."
echo "It will auto-start on reboot."
echo ""
echo "Useful commands:"
echo "  sudo systemctl status fctool    # Check status"
echo "  sudo systemctl restart fctool   # Restart"
echo "  sudo systemctl stop fctool      # Stop"
echo "  journalctl -u fctool -f         # View live logs"
echo "  journalctl -u fctool --since today  # Today's logs"
echo ""
