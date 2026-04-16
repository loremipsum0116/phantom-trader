#!/bin/bash
# Phantom Trader Dashboard deployment helper
# Run on the target Linux VM after placing the project under $HOME/phantom_trader

set -e

PROJ_DIR="$HOME/phantom_trader"
DASH_DIR="$PROJ_DIR/dashboard"
VENV="$PROJ_DIR/venv"

echo "=== Phantom Trader Dashboard Deployment ==="

mkdir -p "$DASH_DIR/static"
echo "[1/5] Dashboard directory ready"

if [ ! -f "$DASH_DIR/api.py" ]; then
    echo "ERROR: $DASH_DIR/api.py was not found. Upload the project first."
    exit 1
fi

if [ ! -f "$PROJ_DIR/.env" ]; then
    echo "ERROR: $PROJ_DIR/.env was not found."
    echo "Copy .env.example to .env and set DASHBOARD_PASSWORD / DASHBOARD_SECRET first."
    exit 1
fi

echo "[2/5] Installing dashboard dependencies"
source "$VENV/bin/activate"
pip install fastapi 'uvicorn[standard]' --quiet

echo "[3/5] Installing systemd service"
sudo cp "$DASH_DIR/phantom_dashboard.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable phantom_dashboard

echo "[4/5] Opening local firewall port if ufw is enabled"
sudo ufw allow 8080/tcp 2>/dev/null || true

echo "[5/5] Restarting service"
sudo systemctl restart phantom_dashboard
sleep 2
sudo systemctl status phantom_dashboard --no-pager

echo ""
echo "Dashboard service deployed."
echo "Review your cloud firewall/security-group settings separately before exposing port 8080."
echo "Make sure DASHBOARD_PASSWORD and DASHBOARD_SECRET are set in .env."
