#!/bin/bash
# Run by the weekly schedule (see schedule.sh). Checks for new meetings, processes any,
# logs everything to logs/run.log, and pops up a notification only when something happened.
cd "$(dirname "$0")"
mkdir -p logs
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

{
  echo
  echo "===== $(date) ====="
  bash run.sh
} >> logs/run.log 2>&1
STATUS=$?

notify() { osascript -e "display notification \"$1\" with title \"Yarmouth minutes\"" ; }
LAST_RUN="$(awk '/^===== /{buf=""} {buf=buf"\n"$0} END{print buf}' logs/run.log)"

if [ $STATUS -ne 0 ]; then
  if echo "$LAST_RUN" | grep -qiE "401|authentication"; then
    notify "Run failed: API key expired. Run: bash run.sh --new-key"
  else
    notify "Weekly run failed. See logs/run.log in yarmouth-minutes."
  fi
elif echo "$LAST_RUN" | grep -q "Pushed new"; then
  notify "New meeting summary is on GitHub."
fi
exit $STATUS
