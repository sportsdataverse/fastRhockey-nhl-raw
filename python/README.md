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

## Run the scraper

```sh
# one game -> raw/{id}.json + final/{id}.json (enriched; --models enables xG)
uv run python -m nhl_raw.scrape 2024020001 --out-dir nhl/json --models ../../fastRhockey-nhl-data/models
```

## Port status (parity-gated)

- [x] **SP-B** parse / coordinates / shot geometry (`test_feed_parity.py`)
- [x] **SP-C** shift integration (on-ice cumsum) + strength states — on-ice 294/294, rows 850/850
- [x] **SP-D** descriptions + finalize + xG — xG 90/90 within 4.9e-5 (jsonlite rounding limit)
- [x] **SP-E** raw-json assembly + boxscore + fetch + shifts + scrape driver
  - validated **live end-to-end** (fetch api-web + shiftcharts → final): all_plays 850/850,
    shifts 501/501, on-ice 90/90, xG 90/90
- [x] **schedule + season loop** (`nhl_schedule` / `scrape_season`, `-s/-e` CLI) — live: FLA
  2023-24 = 82 R + 24 playoff games
- [x] **HTML-TOI shift fallback** (`parse_toi_html`) for empty-shiftcharts games — live: the
  HTML path aggregates to the same 501 CHANGE rows as the JSON path

The NHL **scraper** side is complete. Remaining for full NHL self-sufficiency: the
**reshaper** port (`fastRhockey-nhl-data` → Python, 15 datasets → `sportsdataverse-data`
`nhl_*` releases).

22 hermetic parity tests (`uv run pytest`), ruff clean.
