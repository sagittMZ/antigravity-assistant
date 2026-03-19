#!/usr/bin/env bash
# install-service.sh — Install Antigravity Assistant as a user-level systemd service.
#
# This uses systemd --user, so no sudo is needed.
# After installation the service will auto-start on every login
# (and even without login, thanks to enable-linger).
#
# Usage:
#   cd ~/antigravity-assistant
#   bash scripts/install-service.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="antigravity-assistant"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ Virtual environment not found at $VENV_PYTHON"
    echo "   Run: python3 -m venv $PROJECT_DIR/venv && source $PROJECT_DIR/venv/bin/activate && pip install -r $PROJECT_DIR/requirements.txt"
    exit 1
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$SERVICE_NAME.service" << EOF
[Unit]
Description=Antigravity Assistant (launcher + TG bot + services)
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -m app.launcher
Restart=on-failure
RestartSec=10
# Load environment variables from .env
EnvironmentFile=$PROJECT_DIR/.env
# Antigravity IDE needs DISPLAY to render its window
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
EOF

echo "✅ Service unit created: $UNIT_DIR/$SERVICE_NAME.service"

# Reload systemd
systemctl --user daemon-reload

# Enable the service (auto-start on login)
systemctl --user enable "$SERVICE_NAME"
echo "✅ Service enabled (will auto-start on login)."

# Enable lingering so the service starts even without a graphical login session
# (e.g. after a reboot where auto-login is enabled)
loginctl enable-linger "$USER"
echo "✅ Linger enabled for user $USER."

echo ""
echo "Commands:"
echo "   systemctl --user start $SERVICE_NAME     # Start now"
echo "   systemctl --user stop $SERVICE_NAME      # Stop"
echo "   systemctl --user restart $SERVICE_NAME   # Restart"
echo "   systemctl --user status $SERVICE_NAME    # Check status"
echo "   journalctl --user -u $SERVICE_NAME -f    # Follow logs"
echo ""
echo "After a reboot, the service will start automatically."
echo "No terminal windows needed."
