#!/bin/bash
# One-time setup on a Mac: installs the one Python package needed and schedules a daily run at 6:15pm.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
"$PY" -m pip install --user --quiet requests
PLIST="$HOME/Library/LaunchAgents/com.stockscreener.senate.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stockscreener.senate</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/senate_trades.py</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>$DIR/senate.log</string>
  <key>StandardErrorPath</key><string>$DIR/senate.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Scheduled daily at 6:15pm. Log: $DIR/senate.log"
echo "Run it once now with:  $PY \"$DIR/senate_trades.py\""
