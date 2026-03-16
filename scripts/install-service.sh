#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$HOME/antigravity-assistant"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="antigravity-assistant"

mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/$SERVICE_NAME.service" << EOF
[Unit]
Description=Antigravity Assistant
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -m app.launcher
Restart=on-failure
RestartSec=10
EnvironmentFile=$PROJECT_DIR/.env

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "✅ Service was created."
echo "   systemctl --user start $SERVICE_NAME"
echo "   systemctl --user enable $SERVICE_NAME"
echo "   loginctl enable-linger $USER"
