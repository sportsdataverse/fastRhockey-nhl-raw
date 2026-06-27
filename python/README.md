# nhl-raw (Python)

Python port of the `fastRhockey-nhl-raw` scraper + PBP enrichment, so the NHL data
pipeline can run self-sufficiently without the R `fastRhockey` package.

It reproduces, from the live NHL api-web endpoints:

- `nhl/json/raw/{game_id}.json`  — the four raw api-web responses organized under the
  old-format keys (`all_plays`, `linescore`, `decisions`, `scoring`, `penalties`,
  `officials`, `scratches`, `shots_by_period`, `shootout`, `shifts`, …).
- `nhl/json/final/{game_id}.json` — the **enriched** output: event-level PBP with fixed
  coordinates, shot distance/angle, on-ice skaters, strength states, descriptions, and
  expected goals (xG); plus parsed boxscore stats.

Canonical R sources being ported (faithful, parity-gated):

| R | Python |
|---|---|
| `fastRhockey/R/nhl_game_feed.R` (`.parse_plays`/`.fix_coordinates`/`.add_shot_metrics`/`.integrate_shifts`/`.add_strength_states`/`.add_descriptions`) | `nhl_raw/feed.py` |
| `fastRhockey/R/nhl_game_shifts.R` | `nhl_raw/shifts.py` *(SP-C)* |
| `fastRhockey/R/nhl_xg.R` (`helper_nhl_calculate_xg`) | `nhl_raw/xg.py` *(SP-D)* |
| `fastRhockey/R/nhl_game_boxscore.R` | `nhl_raw/boxscore.py` *(SP-E)* |
| `fastRhockey-nhl-raw/R/scrape_nhl_raw.R` (`build_raw_json`/`download_game`/CLI) | `nhl_raw/assemble.py` + `nhl_raw/scrape.py` *(SP-E)* |

## Dev

```sh
uv run --with polars --with pandas --with pyarrow --with pytest python -m pytest -q
```

## Port status (parity-gated)

- [x] **SP-B** parse / coordinates / shot geometry (`test_feed_parity.py`)
- [ ] SP-C shift integration + strength states
- [ ] SP-D descriptions + finalize + xG
- [ ] SP-E raw-json assembly + boxscore + schedule + scrape CLI
