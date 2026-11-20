#!/bin/bash
# Install (or remove) the Mac runner watchdog: a launchd agent that runs mac-runner-watchdog.sh every 2 minutes as
# this user (the one that owns Colima and the runner containers). Idempotent: run it again to update.
#   bash install-mac-runner-watchdog.sh            install or update
#   bash install-mac-runner-watchdog.sh --remove   uninstall (its log and state are kept)
set -euo pipefail
LABEL=local.mac-runner-watchdog
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN="$HOME/.local/bin/mac-runner-watchdog.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$PLIST" "$BIN"
  echo "removed $LABEL"
  exit 0
fi
mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
install -m 755 "$HERE/mac-runner-watchdog.sh" "$BIN"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$BIN</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/mac-runner-watchdog.err.log</string>
</dict>
</plist>
PLIST_EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (every 2 minutes, and at login)"
echo "log:    tail -f ~/Library/Logs/mac-runner-watchdog.log"
echo "status: $BIN --status"
