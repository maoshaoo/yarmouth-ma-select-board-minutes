#!/bin/bash
# Run the Yarmouth minutes pipeline on this Mac, then push results to GitHub.
#
#   bash run.sh               process new meetings (up to max_per_run in config.toml)
#   bash run.sh --probe       just list meetings it can see
#   bash run.sh --show 5165   process one meeting by its show number
#   bash run.sh --new-key     replace the saved Anthropic API key (e.g. after it expires)
set -uo pipefail
cd "$(dirname "$0")"

# Keep local-only files (Python env, API key) out of GitHub.
[ -f .gitignore ] || printf '.venv/\n.env\n__pycache__/\n' > .gitignore

# 1. Python environment (set up on first run, refreshed when requirements change)
PY="$(brew --prefix python@3.12)/bin/python3.12"
if [ ! -d .venv ]; then
  echo "First run: setting up Python (about a minute)..."
  "$PY" -m venv .venv || exit 1
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt || exit 1
  touch .venv/.installed
fi

# 2. API key, saved only on this Mac (in .env, which is never uploaded)
if [ "${1:-}" = "--new-key" ]; then
  rm -f .env
  shift
fi
if [ ! -f .env ]; then
  read -rsp "Paste your Anthropic API key (it won't show on screen), then press Enter: " KEY
  echo
  echo "ANTHROPIC_API_KEY=$KEY" > .env
  chmod 600 .env
fi
set -a; source .env; set +a

# 3. Get the latest from GitHub, run (keeping the Mac awake), push results
git pull -q --rebase || { echo "Couldn't pull from GitHub."; exit 1; }
caffeinate -i .venv/bin/python pipeline.py "$@"
STATUS=$?

# Save results to GitHub (only the paths that exist, so one missing file can't block the rest).
for p in .gitignore raw summary README.md processed.json; do
  [ -e "$p" ] && git add "$p"
done
if ! git diff --cached --quiet; then
  git config user.name >/dev/null || git config user.name "yarmouth-minutes"
  git config user.email >/dev/null || git config user.email "yarmouth-minutes@users.noreply.github.com"
  if git commit -q -m "Add Yarmouth meeting summaries $(date +%F)" && git push -q; then
    echo "Pushed new transcripts/summaries to GitHub."
  else
    echo "Couldn't save to GitHub (see the message above)."
  fi
fi

if [ $STATUS -ne 0 ]; then
  echo
  echo "Some meetings failed (see above). If it mentions 'authentication' or '401',"
  echo "your API key has expired: run  bash run.sh --new-key"
fi
exit $STATUS
