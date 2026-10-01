# ops/ — local-only glue

Scripts and launchd jobs that run on Ke's Mac, not in GitHub Actions.

## Why

After PR #4 (merged 2026-09-30) the FedWatch scrape no longer runs in CI —
CME blocks GitHub Actions datacenter IPs at the protocol level. The scrape
now runs from a residential IP on Ke's Mac. These files are the automation
glue that keeps that reliable:

- `fedwatch_daily.sh` — wrapper script. Pulls repo, runs `src.fedwatch`,
  commits + pushes `data/observations.db` if it changed.
- `com.akp.polymarket.fedwatch.plist` — launchd job. Fires the wrapper
  Mon–Fri at 15:30 local time (matches the retired Actions cadence).

## Install (first time)

```bash
cp ops/com.akp.polymarket.fedwatch.plist ~/Library/LaunchAgents/
launchctl load   ~/Library/LaunchAgents/com.akp.polymarket.fedwatch.plist
launchctl start  com.akp.polymarket.fedwatch         # force a run now
tail -f /tmp/akp-polymarket-fedwatch.log             # watch it work
```

## Verify it's loaded

```bash
launchctl list | grep akp.polymarket
# Expect something like:
#   -   0   com.akp.polymarket.fedwatch
# (PID empty between runs; exit code 0 after the last run)
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.akp.polymarket.fedwatch.plist
rm               ~/Library/LaunchAgents/com.akp.polymarket.fedwatch.plist
```

## Backstop

`src/health.py`'s `fedwatch-staleness` check runs in the `health` workflow
every 2h. If the newest `cme_fedwatch` row in `comparables` is older than 36h
(one business day + overnight slack), it opens a GitHub issue labelled
`health`. So "Mac was off for a week" is loud, not silent.
