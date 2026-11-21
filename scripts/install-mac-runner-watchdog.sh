#!/bin/bash
# Install (or remove) the Mac runner watchdog: a launchd agent that runs mac-runner-watchdog.sh every 2 minutes as
# this user (the one that owns Colima and the runner containers), with the load governor (mac-runner-governor.sh)
# beside it. Idempotent: run it again to update. Governor settings (GOV_*, see the top of mac-runner-governor.sh)
# can be given on the command line and are written into the agent, e.g.
#   GOV_GH_ORGS='sconsinc:SConsInc' bash install-mac-runner-watchdog.sh
#   bash install-mac-runner-watchdog.sh            install or update
#   bash install-mac-runner-watchdog.sh --remove   uninstall (its log and state are kept)
set -euo pipefail
LABEL=local.mac-runner-watchdog
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN="$HOME/.local/bin/mac-runner-watchdog.sh"
GOV="$HOME/.local/bin/mac-runner-governor.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if [ "${1:-}" = "--remove" ]; then
  [ -f "$GOV" ] && bash "$GOV" --resume-all || true  # nothing stays paused once nothing watches it
  rm -f "$PLIST" "$BIN" "$GOV"
  echo "removed $LABEL"
  exit 0
fi
mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
install -m 755 "$HERE/mac-runner-watchdog.sh" "$BIN"
install -m 755 "$HERE/mac-runner-governor.sh" "$GOV"
# Settings for the agent: every GOV_* and WATCHDOG_* variable set when this was run.
ENVXML=""
for v in $(env | grep -oE '^(GOV|WATCHDOG)_[A-Z_]+' | sort -u); do
  val="$(printenv "$v" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
  ENVXML="$ENVXML    <key>$v</key><string>$val</string>
"
done
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$BIN</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
$ENVXML  </dict>
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
echo "load governor: on (WATCHDOG_GOVERNOR=0 bash $0 turns it off); undo every pause: $GOV --resume-all"
