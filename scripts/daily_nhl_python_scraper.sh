#!/bin/bash
# Scrape raw NHL game JSON via the python/nhl_raw port (R sibling: daily_nhl_scraper.sh).
# Usage: bash scripts/daily_nhl_python_scraper.sh -s 2026 -e 2026 -r TRUE
# Season years are END years (2026 = 2025-26), same as the R path.
#
# Writes nhl/json/raw/{id}.json + nhl/json/final/{id}.json (enriched, incl. xG via
# the download-on-first-use canonical models). Unlike the R path it does NOT write
# nhl/schedules/ or nhl/nhl_schedule_master.* -- the python port has no schedule
# persistence yet, so the R path remains the default until it does.

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
NO_RESCRAPE=""
case "$RESCRAPE" in
    TRUE|true|True) ;;
    *) NO_RESCRAPE="--no-rescrape";;
esac
mkdir -p logs
for i in $(seq "${START_YEAR}" "${END_YEAR}")
do
    LOGFILE="logs/fastRhockey_nhl_raw_logfile_${i}.log"
    TMPLOG=$(mktemp "/tmp/fastRhockey_nhl_raw_logfile_${i}.XXXXXX.log")
    echo "=== Processing season $i (python) ==="
    # Tee inside the block writes to /tmp (untracked) so the `git pull` calls
    # don't trip over their own log output being written to a tracked file.
    {
        git pull >> /dev/null
        git config --local user.email "action@github.com"
        git config --local user.name "Github Action"
        # --group xg pulls xgboost+numpy (not in the default groups); cwd=python
        # puts the nhl_raw package on sys.path, so out-dir points back up a level.
        (cd python && uv run --group xg python -m nhl_raw.scrape -s "$i" -e "$i" --out-dir ../nhl/json $NO_RESCRAPE)
        git pull >> /dev/null
        git add nhl/ >> /dev/null
        git commit -m "NHL Raw Updated (Start: $i End: $i)" || echo "No changes to commit"
        git pull >> /dev/null
        git push >> /dev/null
    } 2>&1 | tee "$TMPLOG"

    # Block is finished and pushed; tee has closed $TMPLOG. Now copy the log
    # into its tracked location and commit/push it on its own.
    cp "$TMPLOG" "$LOGFILE"
    git pull --rebase >> /dev/null || true
    git add "$LOGFILE"
    git commit -m "NHL Raw log update (Start: $i End: $i)" >> /dev/null || echo "No log changes to commit"
    git push >> /dev/null
    rm -f "$TMPLOG"
done
