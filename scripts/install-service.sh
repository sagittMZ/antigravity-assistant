#!/usr/bin/env bash
# scripts/install-service.sh — Install Antigravity Assistant as a
# user-level systemd service (no sudo required).
#
# QA FIX: Added MemoryMax=1500M and MemorySwapMax=0M to prevent system-wide lockups.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="antigravity-assistant"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ Virtual environment not found at $VENV_PYTHON"
    echo "   Run: python3 -m venv $PROJECT_DIR/venv"
    echo "        source $PROJECT_DIR/venv/bin/activate"
    echo "        pip install -r $PROJECT_DIR/requirements.txt"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "❌ .env file not found at $PROJECT_DIR/.env"
    echo "   Run: cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env"
    echo "   Then fill in BOT_TOKEN and ALLOWED_USER_ID."
    exit 1
fi

if ! grep -q "BOT_TOKEN=.\+" "$PROJECT_DIR/.env" 2>/dev/null; then
    echo "⚠️  BOT_TOKEN looks empty in .env — please fill it in before starting."
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$SERVICE_NAME.service" << EOF
[Unit]
Description=Antigravity Assistant (launcher + TG bot + services)
After=network-online.target graphical-session-pre.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -m app.launcher
Restart=on-failure
RestartSec=10

# QA FIX: Hard memory limits to prevent OS Swap Death
MemoryMax=1500M
MemorySwapMax=0M
OOMPolicy=restart

EnvironmentFile=$PROJECT_DIR/.env
Environment=DISPLAY=:0
Environment=PYTHONUNBUFFERED=1

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

echo "✅ Service unit created: $UNIT_DIR/$SERVICE_NAME.service"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
echo "✅ Service enabled (auto-start on login)."

loginctl enable-linger "$USER"
echo "✅ Linger enabled for user: $USER"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Useful commands:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Start now:    systemctl --user start $SERVICE_NAME"
echo "  Stop:         systemctl --user stop $SERVICE_NAME"
echo "  Restart:      systemctl --user restart $SERVICE_NAME"
echo "  Status:       systemctl --user status $SERVICE_NAME"
echo "  Live logs:    journalctl --user -u $SERVICE_NAME -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

read -r -p "Start the service now? [y/N] " response
if [[ "$response" =~ ^[Yy]$ ]]; then
    systemctl --user start "$SERVICE_NAME"
    echo "✅ Service started."
    echo "   Watch logs: journalctl --user -u $SERVICE_NAME -f"
else
    echo "ℹ️  Service not started. Run manually when ready:"
    echo "   systemctl --user start $SERVICE_NAME"
fi