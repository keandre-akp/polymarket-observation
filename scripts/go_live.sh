#!/usr/bin/env bash
# One-shot bring-up for the observation pipeline.
#
# Safe to re-run: every step is idempotent. Stops at the first failure so a
# broken step never cascades into a half-populated log.
#
#   bash scripts/go_live.sh          # discover, poll, verify -- writes nothing to Notion
#   bash scripts/go_live.sh --sync   # ... then sync to Notion for real
set -euo pipefail

cd "$(dirname "$0")/.."
SYNC="${1:-}"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if [[ -z "${VIRTUAL_ENV:-}" && -d .venv ]]; then
  step "Activating .venv"
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

step "1/5  Discovering the seven observation events"
python -m src.poller discover

step "2/5  Taking a price snapshot"
python -m src.poller poll

step "3/5  Checking every track rule resolved to exactly one sub-market"
python -m src.notion_sync --link-report

step "4/5  Checking the Notion schema and integration access"
if [[ -z "${NOTION_API_KEY:-}" ]]; then
  echo "NOTION_API_KEY not set -- skipping. Export it to run the Notion steps."
  exit 0
fi
python -m src.notion_sync --verify-schema

if [[ "$SYNC" != "--sync" ]]; then
  step "5/5  Dry run (no writes)"
  python -m src.notion_sync --dry-run | head -60
  echo
  echo "Looks right? Re-run with:  bash scripts/go_live.sh --sync"
  exit 0
fi

step "5/5  Syncing to Notion"
python -m src.notion_sync
python -m src.resolve --dry-run
echo
echo "Done. Nothing has been logged as resolved yet -- that was a dry run."
echo "Once the CPI markets settle, run:  python -m src.resolve"
