"""NHL season schedule — Python port of ``fastRhockey::nhl_schedule`` (season path).

Canonical R source: ``fastRhockey/R/nhl_schedule.R`` — ``nhl_schedule`` /
``.parse_club_schedule_games``. The scraper only needs the season's game ids + states, so
this ports the per-team ``club-schedule-season`` union (regular + playoff games appear in
the participating teams' club schedules). R fetches playoffs separately via the playoff
carousel for richer series context (``series_letter`` / ``playoff_round``) the scraper
does not use -- those three columns are derived from the playoff game id instead
(``_playoff_context_exprs``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from nhl_raw.fetch import FetchError, get_json

_CLUB_SCHEDULE = "https://api-web.nhle.com/v1/club-schedule-season/{team}/{season}"
_GAME_TYPE = {1: "PR", 2: "R", 3: "P", 4: "A"}

# Current franchises (the R fallback list when nhl_team_logos is unavailable).
_TEAMS = [
    "ANA",
    "BOS",
    "BUF",
    "CAR",
    "CBJ",
    "CGY",
    "CHI",
    "COL",
    "DAL",
    "DET",
    "EDM",
    "FLA",
    "LAK",
    "MIN",
    "MTL",
    "NJD",
    "NSH",
    "NYI",
    "NYR",
    "OTT",
    "PHI",
    "PIT",
    "SEA",
    "SJS",
    "STL",
    "TBL",
    "TOR",
    "UTA",
    "VAN",
    "VGK",
    "WPG",
    "WSH",
]

# R's nhl_schedule() types (integer -> int32) -- every R-written season is int32, and
# write_schedule_parquet keeps that on disk.
_SCHEDULE_SCHEMA = {
    "game_id": pl.Int32,
    "season_full": pl.Utf8,
    "game_type": pl.Utf8,
    "game_date": pl.Utf8,
    "game_time": pl.Utf8,
    "home_team_abbr": pl.Utf8,
    "away_team_abbr": pl.Utf8,
    "home_team_name": pl.Utf8,
    "away_team_name": pl.Utf8,
    "home_score": pl.Int32,
    "away_score": pl.Int32,
    "game_state": pl.Utf8,
    "venue": pl.Utf8,
}


def _parse_game(g: dict) -> dict:
    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}

    def place(t: dict) -> str | None:
        return (t.get("placeName") or {}).get("default")

    return {
        "game_id": g.get("id"),
        "season_full": str(g.get("season")) if g.get("season") is not None else None,
        "game_type": _GAME_TYPE.get(g.get("gameType")),
        "game_date": g.get("gameDate"),
        "game_time": g.get("startTimeUTC"),
        "home_team_abbr": home.get("abbrev"),
        "away_team_abbr": away.get("abbrev"),
        "home_team_name": place(home),
        "away_team_name": place(away),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "game_state": g.get("gameState"),
        "venue": (g.get("venue") or {}).get("default"),
    }


def nhl_schedule(season: int, *, teams: list[str] | None = None, session: object | None = None) -> pl.DataFrame:
    """Season schedule (regular + playoff) as a tidy frame; ``season`` is the end year."""
    season_str = f"{season - 1}{season}"
    by_id: dict[int, dict] = {}
    failed: list[str] = []
    absent: list[str] = []
    for tm in teams or _TEAMS:
        # strict: _TEAMS is the CURRENT 32, so a 404 is expected for a franchise that
        # did not exist in this season (SEA before 2022, UTA before 2025, VGK before
        # 2018) -- that is an absent team, not a failure, and must not be reported as
        # one. Anything else raises.
        try:
            raw = get_json(_CLUB_SCHEDULE.format(team=tm, season=season_str), session=session, strict=True)
        except FetchError as exc:
            failed.append(f"{tm} ({exc})")
            continue
        if raw is None:
            absent.append(tm)
            continue
        for g in raw.get("games") or []:
            if g.get("id") is not None:
                by_id[g["id"]] = g
    if absent:
        print(
            f"nhl_schedule({season_str}): no club schedule for {', '.join(absent)} (expected pre-expansion)",
            file=sys.stderr,
        )
    if failed:
        # RAISE, do not warn-and-continue. This schedule decides which games get
        # scraped, so returning it short means those teams' games are never fetched
        # and the run still reports success -- a silently incomplete season.
        raise FetchError(
            f"nhl_schedule({season_str}): club-schedule fetch FAILED for {len(failed)} team(s): "
            f"{'; '.join(failed)}. Refusing to return a partial schedule."
        )
    if not by_id and teams is None:
        # Only on a FULL-LEAGUE scan. An explicit `teams=` subset can legitimately
        # have no games (SEA in 2015), and concluding "the league is down" from a
        # sample of one is the same flaw the game loop just fixed with `>= 100 ids`.
        #
        # ZERO games across every team is the refusal -- NOT "every team 404'd". The
        # host does not 404 a pre-expansion team-season: SEA/20142015, VGK/20142015
        # and UTA/20222023 all return 200 with `games: []`. An absent-count guard
        # therefore misses the shape that actually occurs, which is exactly what an
        # earlier version of this guard did. A genuinely unpublished future season is
        # already covered elsewhere -- those return 500, so the all-FAILED refusal
        # above fires first.
        #
        # Refusing matters because the alternative is an empty frame, ids=[], the
        # game loop's own guard skipped by that empty list, and a run that exits 0
        # having written nothing. The schedule and the play-by-play are the same
        # host, so this is where an outage lands first.
        raise FetchError(
            f"nhl_schedule({season_str}): no games from any of {len(teams or _TEAMS)} team(s) "
            f"({len(absent)} returned no schedule) -- refusing to report an empty season"
        )
    df = pl.DataFrame([_parse_game(g) for g in by_id.values()], schema=_SCHEDULE_SCHEMA)
    # Keep regular + playoff (drop preseason PR / all-star A), mirror R's regular+playoff union.
    df = df.filter(pl.col("game_type").is_in(["R", "P"])).sort(["game_date", "game_id"])
    return df.with_columns(_playoff_context_exprs())


# Round -> offset of its first series letter (R1 A-H, R2 I-L, R3 M-N, Final O; the 2020
# bubble's qualifying round 0 starts at R). Matches R's carousel-derived values on every
# playoff game 2011-2026.
_ROUND_LETTER_OFFSET = {0: 17, 1: 0, 2: 8, 3: 12, 4: 14}
_LETTERS = {i: chr(ord("A") + i) for i in range(26)}


def _playoff_context_exprs() -> list[pl.Expr]:
    """R's series_letter / playoff_round / series_game_number, read off the playoff
    game id (``SSSS03RMG``: round, matchup, game) instead of R's carousel + per-series
    fetches -- the id carries all three. Null on non-playoff rows, as in R."""
    gid = pl.col("game_id").cast(pl.Utf8)
    is_p = pl.col("game_type") == "P"
    rnd = gid.str.slice(7, 1).cast(pl.Int32)
    matchup = gid.str.slice(8, 1).cast(pl.Int32)
    letter_code = rnd.replace_strict(_ROUND_LETTER_OFFSET, default=None, return_dtype=pl.Int32) + matchup - 1
    return [
        pl.when(is_p)
        .then(letter_code.replace_strict(_LETTERS, default=None, return_dtype=pl.Utf8))
        .alias("series_letter"),
        pl.when(is_p).then(rnd).alias("playoff_round"),
        pl.when(is_p).then(gid.str.slice(9, 1).cast(pl.Int32)).alias("series_game_number"),
    ]


_RAW_FINAL_URL = "https://raw.githubusercontent.com/sportsdataverse/fastRhockey-nhl-raw/main/nhl/json/final"


def write_schedule_parquet(schedule: pl.DataFrame, path: Path, season: int, final_ids: set[int]) -> None:
    """Port of ``scrape_nhl_raw.R``'s schedule write: add ``season`` + the
    ``game_json`` / ``game_json_url`` links, and keep R's arrow types (int32, string)
    so the 2026-27+ files match every R-written season."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = schedule.with_columns(season=pl.lit(season, dtype=pl.Int32)).with_columns(
        game_json=pl.col("game_id").is_in(sorted(final_ids)),
    )
    df = df.with_columns(
        game_json_url=pl.when(pl.col("game_json")).then(
            pl.format("{}/{}.json", pl.lit(_RAW_FINAL_URL), pl.col("game_id"))
        )
    )
    tbl = df.to_arrow()
    tbl = tbl.cast(
        pa.schema(
            [
                pa.field(
                    f.name,
                    pa.string()
                    if pa.types.is_large_string(f.type)
                    else pa.int32()
                    if pa.types.is_integer(f.type)
                    else f.type,
                )
                for f in tbl.schema
            ]
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, path, compression="gzip")


def completed_game_ids(season: int, *, session: object | None = None) -> list[int]:
    """Game ids for the season's completed (``game_state == 'OFF'``) games — scraper input."""
    sched = nhl_schedule(season, session=session)
    return sched.filter(pl.col("game_state") == "OFF")["game_id"].to_list()
