# NHL raw/final parity fixtures

Hermetic oracle for the Python port of the NHL enrichment pipeline. Each game has:

- `raw_{gid}.json`  — the committed `nhl/json/raw/{gid}.json` (stores the four raw
  api-web responses `pbp_raw`/`boxscore_raw`/`landing_raw`/`right_rail_raw` + parsed
  `shifts`). This is the **input**.
- `final_{gid}.json` — the committed `nhl/json/final/{gid}.json` (R's enriched output:
  `all_plays`, `game_info`, `rosters`, `team_box_parsed`, `skater_stats`,
  `goalie_stats`). This is the **oracle**.

Because the raw responses are stored alongside the enriched output, parity tests feed
the Python port the *same* `pbp_raw`/`shifts` the R `nhl_game_feed` saw and assert it
reproduces `final.all_plays` — no live fetch, fully deterministic.

| game | season | note |
|---|---|---|
| `2024020001` | 2024-25 | modern game, 850 enriched plays (incl. 501 CHANGE), full coords/strength/on-ice/xG — the enrichment oracle |
| `2009020714` | 2009-10 | api-web has **no** PBP this far back (`all_plays` empty) — boxscore/roster/raw-assembly parity only |

Provenance: copied verbatim from `sportsdataverse/fastRhockey-nhl-raw@main`
(`nhl/json/{raw,final}/`). R producer: `fastRhockey::nhl_game_feed` / `nhl_game_boxscore`.

`boxscore_2026020305.json` (BUF at CHI, 2025-26) is not a raw/final pair: it is the
`boxscore_raw` block of the committed `nhl/json/raw/2026020305.json`, trimmed to the game
header, both teams' ids/names/score, two forwards and one defenseman a side (one of them the
unspaced `"J.Kulich"` the API sends) and all four goalies. `test_boxscore_player_names.py`
parses it. The same file is fastRhockey's `tests/testthat/fixtures/nhl/boxscore_2026020305.json`.
