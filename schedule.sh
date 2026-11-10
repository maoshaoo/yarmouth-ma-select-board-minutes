#!/bin/bash
# Turn the weekly Mac schedule on or off.
#
#   bash schedule.sh          turn it on (Thursdays at 6:00pm by default)
#   bash schedule.sh off      turn it off
#
# The Select Board meets on Tuesdays and the video usually posts a day or two later.
# If the Mac is asleep at 6pm Thursday, macOS runs it the next time the Mac wakes up.
DAY=4      # 0=Sunday ... 4=Thursday
HOUR=18    # 24-hour clock
MINUTE=0

LABEL="com.yarmouth-minutes.weekly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
if [ "${1:-}" = "off" ]; then
  rm -f "$PLIST"
  echo "Weekly schedule turned off."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/scheduled-run.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>$DAY</integer>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST" && \
  echo "Weekly schedule is on: Thursdays at $(printf '%d:%02d' $HOUR $MINUTE) (24h). Log: $DIR/logs/run.log"
