#!/usr/bin/env bash
# Commit data/observations.db back to the repo.
#
# Same contract as the inline block in poll-polymarket-hourly.yml, factored out
# because four workflows now need it. Only the DB is committed: data/fedwatch_raw/
# is gitignored on purpose.
set -euo pipefail

LABEL="${1:-update}"

if [[ -z "$(git status --porcelain data/observations.db)" ]]; then
  echo "No DB changes to commit."
  exit 0
fi

git config user.name "polymarket-bot"
git config user.email "polymarket-bot@users.noreply.github.com"
git add data/observations.db
git commit -m "${LABEL}: $(date -u +%Y-%m-%dT%H:%MZ)"

# The concurrency group should prevent races, but networks are networks.
git push || (git pull --rebase && git push)
