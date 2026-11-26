#!/bin/bash
# Pregame snapshot probe: python/nhl_raw_03_pregame_snapshot.py, one probe per run.
#
# Droplet cron (every 30 min, 15:00-03:00 UTC, October to June; the stage writes nothing on
# a date without NHL games). The 03:00 UTC run also commits the day's file -- once a day,
# not per probe, so the probes never race each other or the daily scrape on a push:
#
#   */30 15-23,0-2 * 10-12,1-6 * cd /mnt/sdv_repos/fastRhockey-nhl-raw && flock -n /tmp/nhl-pregame.lock bash scripts/nhl_pregame_snapshot.sh >> logs/nhl_pregame_snapshot.log 2>&1
#   0 3 * 10-12,1-6 *            cd /mnt/sdv_repos/fastRhockey-nhl-raw && flock -n /tmp/nhl-pregame.lock bash scripts/nhl_pregame_snapshot.sh >> logs/nhl_pregame_snapshot.log 2>&1
#
#   bash scripts/nhl_pregame_snapshot.sh               # one probe of today's ET game date
#   COMMIT=1 bash scripts/nhl_pregame_snapshot.sh      # probe, then commit + push nhl/pregame
#
# Watch it live: tail -f logs/nhl_pregame_snapshot.log

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# cron's PATH has no ~/.local/bin, where uv lives on the droplet
UV="${UV:-$(command -v uv || echo /root/.local/bin/uv)}"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

"$UV" run --frozen python python/nhl_raw_03_pregame_snapshot.py "$@"
RC=$?

if [ "${COMMIT:-0}" = "1" ] || [ "$(date -u +%H)" = "03" ]; then
  git add -- nhl/pregame >/dev/null 2>&1 || true
  if git diff --cached --quiet; then
    echo "nothing to commit under nhl/pregame"
  else
    git commit -m "chore(pregame): NHL pregame snapshot $(TZ=America/New_York date +%Y-%m-%d)" >/dev/null || RC=1
    pushed=0
    for attempt in 1 2 3; do
      if git push origin HEAD >/dev/null 2>&1; then pushed=1; echo "pushed (attempt $attempt)"; break; fi
      echo "push rejected (attempt $attempt); syncing with origin"
      git fetch --quiet origin main || true
      git rebase --merge origin/main >/dev/null 2>&1 || { git rebase --abort >/dev/null 2>&1; break; }
    done
    [ "$pushed" = "1" ] || { echo "::error ::pregame commit did not reach origin"; RC=1; }
  fi
fi

echo "EXIT=$RC"
exit "$RC"
