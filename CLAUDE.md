# CLAUDE.md — fastRhockey-nhl-raw

R-side scraper that pulls raw NHL game payloads from the NHL `api-web` /
stats endpoints (via the [fastRhockey](https://github.com/sportsdataverse/fastRhockey)
package) and commits per-game JSON to this repo. It is the authoritative
raw cache for the NHL pipeline.

Sibling repos: paired downstream parser is
[fastRhockey-nhl-data](https://github.com/sportsdataverse/fastRhockey-nhl-data)
(reshapes this raw cache → release parquet/rds/csv). The PWHL pair is
`fastRhockey-pwhl-raw` / `fastRhockey-pwhl-data`. All four feed the
`fastRhockey` R package's `load_nhl_*()` / `load_pwhl_*()` loaders.
Package name (`DESCRIPTION`): `fastRhockey.nhl.raw` (v1.0.0, MIT).

## Pipeline Position

```
NHL APIs --[R scrape]--> fastRhockey-nhl-raw [HERE]
                              | push -> repository_dispatch (daily_nhl_data)
                              v
                         fastRhockey-nhl-data --[piggyback upload]--> sportsdataverse-data
                                                                          | load_nhl_*()
                                                                          v
                                                                     fastRhockey R package
```

## Commands (verified)

`scripts/daily_nhl_scraper.sh` is the CI / local entry point — it loops
seasons and commits+pushes per season:

```sh
# Full daily flow for one or more seasons
bash scripts/daily_nhl_scraper.sh -s 2026 -e 2026 -r TRUE

# Call the R scraper directly when iterating
Rscript R/scrape_nhl_raw.R -s 2026                # single season (end year 2026 = 2025-26)
Rscript R/scrape_nhl_raw.R -s 2024 -e 2026        # range: 2023-24 through 2025-26
Rscript R/scrape_nhl_raw.R -s 2026 -r TRUE        # rescrape existing files
```

- `-s` / `-e` are the **end year** of the season (2026 = 2025-26),
  matching `fastRhockey:::most_recent_nhl_season()`. `-e` defaults to `-s`.
- `-r TRUE` (default) re-scrapes games already on disk; `-r FALSE` skips
  existing JSON. The shell wrapper defaults `RESCRAPE` to `TRUE`.

## Conventions

- Never add AI co-author trailers to commits.
- This `-raw` repo **commits raw per-game JSON to git on purpose** (the
  SDV raw-cache pattern) — do not warn about repo size or propose moving
  it to external storage.
- Per-season commit subject is `"NHL Raw Updated (Start: $i End: $i)"`,
  and the log commit is `"NHL Raw log update (Start: $i End: $i)"`. The
  downstream data repo greps the dispatched `commit_message` for integers
  (`grep -o -E '[0-9]+'`, first/last), so keep the two season years
  present and outermost in the subject.
- Per-season logs land at `logs/fastRhockey_nhl_raw_logfile_{year}.log`,
  committed separately after the data commit.

## Inputs / Outputs

- **Source:** NHL `api-web` / stats endpoints, fetched through
  `fastRhockey` helpers (`.safe_get_json` wraps `httr::RETRY`).
- **Committed output (consumed downstream):**
  - `nhl/json/raw/{game_id}.json`   — raw API responses, organized under old-format keys (`all_plays`, `player_box`, `linescore`, `decisions`, ...)
  - `nhl/json/final/{game_id}.json` — fully processed via the fastRhockey pipeline (PBP, box scores, game info)
  - `nhl/schedules/{rds,parquet}/nhl_schedule_{end_year}.*` — per-season schedule (`game_json` / `game_json_url` point at `nhl/json/final/`)
  - `nhl/nhl_schedule_master.{rds,parquet}` — combined schedule across all seasons (regenerated each run)
- **Publish target:** none here — this repo only commits to `main`. The
  release upload to `sportsdataverse-data` happens in `fastRhockey-nhl-data`.

## CI Workflows

`.github/workflows/`:

- **`scrape_nhl_raw.yml`** — daily scrape. Cron `0 8 UTC` over the NHL
  calendar (`* 10-12 *`, `* 1-4 *` regular season; `* 5-6 *` playoffs)
  plus `workflow_dispatch` (`start_year` / `end_year` / `rescrape`
  inputs). Empty inputs fall back to `fastRhockey:::most_recent_nhl_season()`.
  Installs `sportsdataverse/fastRhockey`; runs `daily_nhl_scraper.sh`.
- **`fastRhockey_nhl_data_trigger.yml`** — on `[push, workflow_dispatch]`,
  fires a `repository_dispatch` (event-type `daily_nhl_data`, token
  `secrets.SDV_GH_TOKEN`) at `sportsdataverse/fastRhockey-nhl-data`,
  passing `commit_message` in the client payload. That wakes the data
  repo's compile + release upload.

## Project Structure

```
R/scrape_nhl_raw.R                 # single R entry point (schedule + per-game raw/final JSON)
scripts/daily_nhl_scraper.sh       # bash wrapper: loops seasons, commits + pushes per season
nhl/json/raw/, nhl/json/final/     # committed per-game JSON
nhl/schedules/{rds,parquet}/       # per-season schedules
nhl/nhl_schedule_master.{rds,parquet}
logs/                              # per-season scrape logs
.github/workflows/                 # scrape_nhl_raw.yml + fastRhockey_nhl_data_trigger.yml
DESCRIPTION                        # fastRhockey.nhl.raw; Remotes: sportsdataverse/fastRhockey
```

## Gotchas

- `R/scrape_nhl_raw.R` is **the** entry point; all scraping flows through
  `fastRhockey` + the `.safe_get_json` RETRY helper. Don't hard-code
  endpoint URLs in new code — thread them through the fastRhockey helpers.
- `nhl_schedule_master.{rds,parquet}` is rebuilt every run; don't hand-edit.
- The downstream data trigger needs `SDV_GH_TOKEN` (the scrape job itself
  uses the default `GITHUB_TOKEN`). Don't swap those.
