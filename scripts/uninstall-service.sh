#!/usr/bin/env bash
set -Eeuo pipefail

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if command -v systemctl >/dev/null 2>&1; then
  echo "==> Stopping and disabling myrmex-head.service..."
  systemctl --user stop myrmex-head.service 2>/dev/null || true
  systemctl --user disable myrmex-head.service 2>/dev/null || true
fi

echo "==> Removing service unit file..."
rm -f "$SYSTEMD_USER_DIR/myrmex-head.service"

if command -v systemctl >/dev/null 2>&1; then
  echo "==> Reloading systemd user daemon..."
  systemctl --user daemon-reload || true
fi

echo "myrmex-head supervisor uninstalled."
