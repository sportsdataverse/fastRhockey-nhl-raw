# fastRhockey-nhl-raw

Raw NHL game JSON data scraped from the NHL API via [fastRhockey](https://github.com/sportsdataverse/fastRhockey).

```mermaid
  graph LR;
    A[fastRhockey-nhl-raw]-->B[fastRhockey-nhl-data];
    B[fastRhockey-nhl-data]-->C1[nhl_pbp_full];
    B[fastRhockey-nhl-data]-->C2[nhl_pbp_lite];
    B[fastRhockey-nhl-data]-->C3[nhl_team_boxscores];
    B[fastRhockey-nhl-data]-->C4[nhl_player_boxscores];
    B[fastRhockey-nhl-data]-->C5[nhl_rosters];
    B[fastRhockey-nhl-data]-->C6[nhl_schedules];

```

## fastRhockey NHL workflow diagram

```mermaid
flowchart TB;
    subgraph A[fastRhockey-nhl-raw];
        direction TB;
        A1[scripts/daily_nhl_scraper.sh]-->A2[R/scrape_nhl_raw.R];
    end;

    subgraph B[fastRhockey-nhl-data];
        direction TB;
        B1[scripts/daily_nhl_python_processor.sh]-->B2[python -m nhl_data_build.season];
        B3[scripts/daily_nhl_R_processor.sh]-.unscheduled twin.->B4[R/nhl_data_creation.R];
    end;

    subgraph C[sportsdataverse Releases];
        direction TB;
        C1[nhl_pbp_full];
        C2[nhl_pbp_lite];
        C3[nhl_team_boxscores];
        C4[nhl_player_boxscores];
        C5[nhl_rosters];
        C6[nhl_schedules];
    end;

    A-->B;
    B-->C1;
    B-->C2;
    B-->C3;
    B-->C4;
    B-->C5;
    B-->C6;

    click C1 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_pbp_full" _blank;
    click C2 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_pbp_lite" _blank;
    click C3 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_team_boxscores" _blank;
    click C4 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_player_boxscores" _blank;
    click C5 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_rosters" _blank;
    click C6 "https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_schedules" _blank;

```

## sportsdataverse-data releases

| Release tag | Content |
|-----|---------|
| [`nhl_pbp_full`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_pbp_full) | NHL play-by-play data (full, includes shifts) |
| [`nhl_pbp_lite`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_pbp_lite) | NHL play-by-play data (lite, no shift CHANGE events) |
| [`nhl_team_boxscores`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_team_boxscores) | NHL team box scores |
| [`nhl_player_boxscores`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_player_boxscores) | NHL player box scores (skaters + goalies) |
| [`nhl_rosters`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_rosters) | NHL rosters |
| [`nhl_schedules`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nhl_schedules) | NHL schedules |

## Structure

```
nhl/
├── json/
│   ├── raw/              # Raw NHL API responses per game
│   └── final/            # Processed via fastRhockey pipeline (PBP, box scores, game info)
├── schedules/
│   ├── rds/              # Season schedules (nhl_schedule_{year}.rds)
│   └── parquet/          # Season schedules in parquet format
├── nhl_schedule_master.rds       # Combined schedule across all seasons
└── nhl_schedule_master.parquet
```

## Reports & explainers

<!-- BEGIN GENERATED: reports -->

| Report | What it is | Last updated |
|---|---|---|
| _none yet_ | — | — |

<!-- END GENERATED: reports -->

## Automation & status

<!-- BEGIN GENERATED: status -->

| workflow | schedule | last run |
|---|---|---|
| [![fastRhockey_nhl_data_trigger.yml](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/fastRhockey_nhl_data_trigger.yml/badge.svg)](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/fastRhockey_nhl_data_trigger.yml) | on push / dispatch | 2026-07-22 |
| [![orphan_scripts.yml](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/orphan_scripts.yml/badge.svg)](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/orphan_scripts.yml) | on push / PR / dispatch | 2026-08-19 |
| [![scrape_nhl_raw.yml](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/scrape_nhl_raw.yml/badge.svg)](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/scrape_nhl_raw.yml) | daily 08:00 UTC in Oct-Dec; daily 08:00 UTC in Jan-Apr; daily 08:00 UTC in May-Jun | 2026-07-18 |
| [![tests.yml](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/tests.yml/badge.svg)](https://github.com/sportsdataverse/fastRhockey-nhl-raw/actions/workflows/tests.yml) | on push / PR / dispatch | 2026-08-19 |

<!-- END GENERATED: status -->
- **Scraping workflow** runs daily during the NHL season
- On push, triggers the [fastRhockey-nhl-data](https://github.com/sportsdataverse/fastRhockey-nhl-data) repo to compile datasets

## Related repositories

[fastRhockey-nhl-raw data repository (source: NHL API)](https://github.com/sportsdataverse/fastRhockey-nhl-raw)

[fastRhockey-nhl-data repository (source: NHL API)](https://github.com/sportsdataverse/fastRhockey-nhl-data)

[fastRhockey-pwhl-raw data repository (source: HockeyTech API)](https://github.com/sportsdataverse/fastRhockey-pwhl-raw)

[fastRhockey-pwhl-data repository (source: HockeyTech API)](https://github.com/sportsdataverse/fastRhockey-pwhl-data)

[fastRhockey-data legacy repository (archived; sources: NHL Stats API + PHF)](https://github.com/sportsdataverse/fastRhockey-data)

## Part of the [SportsDataverse](https://sportsdataverse.org/)

## Consumers

The packages that read what this repo produces:

- **R:** [fastRhockey](https://fastRhockey.sportsdataverse.org) — docs at <https://fastRhockey.sportsdataverse.org>
- **Python:** [`sportsdataverse.nhl`](https://github.com/sportsdataverse/sportsdataverse-py) — docs at <https://py.sportsdataverse.org>

## Stage inventory

Every numbered pipeline stage in `python/` (auto-listed; run subsets with the `scripts/*.sh` drivers by number or name):

- `python/nhl_raw_01_scrape.py`
