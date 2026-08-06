#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${MYRMEX_BIN_DIR:-$HOME/.local/bin}"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "==> Installing myrmex binaries to $BIN_DIR..."
mkdir -p "$BIN_DIR"
cp "$ROOT/bin/myrmex-campaign" "$BIN_DIR/myrmex-campaign"
cp "$ROOT/bin/myrmex-head" "$BIN_DIR/myrmex-head"
chmod +x "$BIN_DIR/myrmex-campaign" "$BIN_DIR/myrmex-head"

echo "==> Installing systemd user service unit..."
mkdir -p "$SYSTEMD_USER_DIR"
cp "$ROOT/services/myrmex-head.service" "$SYSTEMD_USER_DIR/myrmex-head.service"

if command -v systemctl >/dev/null 2>&1; then
  echo "==> Reloading systemd user daemon..."
  systemctl --user daemon-reload || true
  echo "==> Enabling and starting myrmex-head service..."
  systemctl --user enable myrmex-head.service || true
  systemctl --user start myrmex-head.service || true
  echo "==> Service status:"
  systemctl --user status myrmex-head.service --no-pager || true
fi

echo ""
echo "----------------------------------------------------------------------"
echo "NOTE: To ensure the supervisor survives host restarts without an"
echo "interactive login session, enable linger for your user by running:"
echo "    loginctl enable-linger $USER"
echo "----------------------------------------------------------------------"
echo "myrmex-head supervisor installation complete."
