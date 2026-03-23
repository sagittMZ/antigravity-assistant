#!/usr/bin/env bash
# scripts/install-service.sh — Install Antigravity Assistant as a
# user-level systemd service (no sudo required).
#
# CHANGES vs original:
# - Added StandardOutput=journal and StandardError=journal to the unit file
#   so all output is captured by journald and visible via:
#     journalctl --user -u antigravity-assistant -f
# - Added After=graphical-session-pre.target to handle cases where the
#   display server starts slightly after network-online.target.
# - Added PYTHONUNBUFFERED=1 so Python log lines appear immediately in
#   journald without buffering (critical for live debugging).
# - Improved pre-flight checks: verifies .env exists before installing.
# - Added a "start now" prompt at the end.
#
# Usage:
#   cd ~/antigravity-assistant
#   bash scripts/install-service.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="antigravity-assistant"
UNIT_DIR="$HOME/.config/systemd/user"

# --- Pre-flight checks ---

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

# Check that BOT_TOKEN is set
if ! grep -q "BOT_TOKEN=.\+" "$PROJECT_DIR/.env" 2>/dev/null; then
    echo "⚠️  BOT_TOKEN looks empty in .env — please fill it in before starting."
fi

mkdir -p "$UNIT_DIR"

# --- Write the systemd unit ---

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

# Load environment variables from .env file.
EnvironmentFile=$PROJECT_DIR/.env

# Antigravity IDE requires a running X display.
Environment=DISPLAY=:0

# PYTHONUNBUFFERED=1 ensures log lines appear in journald immediately
# instead of being held in Python's I/O buffer.
Environment=PYTHONUNBUFFERED=1

# Route all stdout/stderr to systemd journal.
# View with: journalctl --user -u $SERVICE_NAME -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

echo "✅ Service unit created: $UNIT_DIR/$SERVICE_NAME.service"

# Reload systemd user daemon
systemctl --user daemon-reload

# Enable auto-start on login
systemctl --user enable "$SERVICE_NAME"
echo "✅ Service enabled (auto-start on login)."

# Enable linger so the service starts even after a reboot without
# interactive login (works with auto-login on ThinkPad).
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
