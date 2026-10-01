#!/bin/zsh
# Daily FedWatch scrape wrapper, invoked by launchd on Ke's Mac.
#
# Why this exists: after PR #4 (merged 2026-09-30) the Actions cron is
# disabled because CME blocks GitHub datacenter IPs. The scrape now runs
# from a residential IP (this Mac). This wrapper is what launchd runs.
#
# Steps per run:
#   1. cd to repo
#   2. git pull --rebase to pick up any Actions commits since last run
#   3. python -m src.fedwatch
#   4. commit + push observations.db IF it changed
#   5. log timestamped stdout/stderr to /tmp/akp-polymarket-fedwatch.log
#
# All output goes to /tmp/akp-polymarket-fedwatch.log so launchd stderr
# is not swallowed. health.py's fedwatch-staleness check will fire a
# GitHub issue if two days pass with no new cme_fedwatch row -- that's
# the backstop if this script silently fails.

set -u    # unset vars are errors
set -o pipefail

REPO="/Users/keandrefoster/Polymarket"
LOG="/tmp/akp-polymarket-fedwatch.log"
PY="/usr/bin/python3"      # same interpreter the manual runs use

exec >> "$LOG" 2>&1
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) fedwatch_daily start ----"

cd "$REPO" || { echo "FATAL: cd $REPO failed"; exit 2; }

git pull --rebase --autostash origin main || {
  echo "WARN: git pull failed, continuing with local state"
}

if ! "$PY" -m src.fedwatch; then
  echo "FATAL: fedwatch scrape failed; not committing"
  exit 3
fi

if git diff --quiet data/observations.db; then
  echo "no observations.db changes to commit"
  echo "---- done ----"
  exit 0
fi

git add data/observations.db
git -c user.name="polymarket-local" \
    -c user.email="polymarket-local@users.noreply.github.com" \
    commit -m "fedwatch: $(date -u +%Y-%m-%dT%H:%MZ) (local)"

# One retry on push race (unlikely with Actions cron off, but cheap insurance)
git push || { git pull --rebase && git push; } || {
  echo "FATAL: git push failed twice"
  exit 4
}

echo "pushed. ---- done ----"
