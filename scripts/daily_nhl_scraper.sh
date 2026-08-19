#!/bin/bash
# Scrape raw NHL game JSON and schedules
# Usage: bash scripts/daily_nhl_scraper.sh -s 2025 -e 2025


# Commit + push, surviving a remote that moved while the build was running.
#
# Pulling BEFORE staging can only abort: the build has just rewritten tracked
# parquet/csv/json, so `git pull` refuses with "Your local changes would be
# overwritten by merge". The old form then committed anyway, pushed into a
# non-fast-forward rejection, and swallowed it -- a GREEN job that published
# nothing (wehoop-wnba-data 32192069433/32192069566, hoopR-nba-data 32204419012).
#
# Stage and commit FIRST so the tree is clean, then reconcile. `rebase --merge`
# rather than `pull --rebase`: the default am backend base64-encodes every blob
# it replays, which crawls on these binary-asset repos.
sdv_commit_push() {
  local msg="$1"; shift
  git add -- "$@" >/dev/null 2>&1 || true
  if git diff --cached --quiet; then
    echo "nothing to commit for: $msg"
    return 0
  fi
  git commit -m "$msg" >/dev/null || { echo "::warning ::commit failed: $msg"; return 1; }
  local attempt
  for attempt in 1 2 3; do
    if git push origin HEAD >/dev/null 2>&1; then
      echo "pushed: $msg (attempt $attempt)"
      return 0
    fi
    echo "push rejected (attempt $attempt); syncing with origin"
    git fetch --quiet origin main || true
    if ! git rebase --merge origin/main >/dev/null 2>&1; then
      git rebase --abort >/dev/null 2>&1 || true
      echo "::error ::cannot rebase onto origin/main for: $msg"
      return 1
    fi
  done
  echo "::error ::push still rejected after 3 attempts: $msg"
  return 1
}

while getopts s:e:r: flag
do
    case "${flag}" in
        s) START_YEAR=${OPTARG};;
        e) END_YEAR=${OPTARG};;
        r) RESCRAPE=${OPTARG};;
    esac
done

RESCRAPE=${RESCRAPE:-TRUE}
echo "Rescrape set to: $RESCRAPE"
mkdir -p logs
for i in $(seq "${START_YEAR}" "${END_YEAR}")
do
    LOGFILE="logs/fastRhockey_nhl_raw_logfile_${i}.log"
    TMPLOG=$(mktemp "/tmp/fastRhockey_nhl_raw_logfile_${i}.XXXXXX.log")
    echo "=== Processing season $i ==="
    # Tee inside the block writes to /tmp (untracked) so the `git pull` calls
    # don't trip over their own log output being written to a tracked file.
    {
        git pull >> /dev/null
        git config --local user.email "action@github.com"
        git config --local user.name "Github Action"
        Rscript R/scrape_nhl_raw.R -s $i -e $i -r $RESCRAPE
        sdv_commit_push "NHL Raw Updated (Start: $i End: $i)" nhl || PUSH_RC=1
    } 2>&1 | tee "$TMPLOG"

    # Block is finished and pushed; tee has closed $TMPLOG. Now copy the log
    # into its tracked location and commit/push it on its own.
    cp "$TMPLOG" "$LOGFILE"
    sdv_commit_push "NHL Raw log update (Start: $i End: $i)" $LOGFILE || PUSH_RC=1
    rm -f "$TMPLOG"
done

# A rejected push is a FAILED run, not a green one. Release assets upload on a
# separate path and can succeed while the repo mirror is left stale.
if [ "${PUSH_RC:-0}" != "0" ]; then
  echo "::error ::At least one commit failed to reach origin; the repo mirror is stale."
  exit 1
fi
