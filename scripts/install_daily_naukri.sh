#!/bin/zsh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.soumik.naukri-agent.plist"
LOG_DIR="$REPO/outbox"

mkdir -p "$PLIST_DIR" "$LOG_DIR"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing virtualenv Python: $PYTHON" >&2
  echo "Create it with: python3 -m venv $REPO/.venv && $PYTHON -m pip install -r $REPO/requirements.txt" >&2
  exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.soumik.naukri-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>src.cli</string>
    <string>naukri</string>
    <string>autopilot</string>
    <string>--scan-queries</string>
    <string>6</string>
    <string>--per-query</string>
    <string>12</string>
    <string>--limit</string>
    <string>10</string>
    <string>--auto-submit</string>
    <string>--slow-mo</string>
    <string>100</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>$REPO</string>
    <key>NAUKRI_CDP_PORT</key>
    <string>9229</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>10</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>ThrottleInterval</key>
  <integer>300</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/daily_naukri.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/daily_naukri.error.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed daily Naukri automation."
echo "Schedule: every day at 10:00 AM local Mac time."
echo "Logs: $LOG_DIR/daily_naukri.log"
echo "Errors: $LOG_DIR/daily_naukri.error.log"
