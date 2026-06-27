"""NHL season schedule — Python port of ``fastRhockey::nhl_schedule`` (season path).

Canonical R source: ``fastRhockey/R/nhl_schedule.R`` — ``nhl_schedule`` /
``.parse_club_schedule_games``. The scraper only needs the season's game ids + states, so
this ports the per-team ``club-schedule-season`` union (regular + playoff games appear in
the participating teams' club schedules). R fetches playoffs separately via the playoff
carousel for richer series context (``series_letter`` / ``playoff_round``) the scraper
does not use — that branch is intentionally omitted here.
"""

from __future__ import annotations

import polars as pl

from nhl_raw.fetch import get_json

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

_SCHEDULE_SCHEMA = {
    "game_id": pl.Int64,
    "season_full": pl.Utf8,
    "game_type": pl.Utf8,
    "game_date": pl.Utf8,
    "game_time": pl.Utf8,
    "home_team_abbr": pl.Utf8,
    "away_team_abbr": pl.Utf8,
    "home_team_name": pl.Utf8,
    "away_team_name": pl.Utf8,
    "home_score": pl.Int64,
    "away_score": pl.Int64,
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
    for tm in teams or _TEAMS:
        raw = get_json(_CLUB_SCHEDULE.format(team=tm, season=season_str), session=session)
        for g in (raw or {}).get("games") or []:
            if g.get("id") is not None:
                by_id[g["id"]] = g
    if not by_id:
        return pl.DataFrame(schema=_SCHEDULE_SCHEMA)
    df = pl.DataFrame([_parse_game(g) for g in by_id.values()], schema=_SCHEDULE_SCHEMA)
    # Keep regular + playoff (drop preseason PR / all-star A), mirror R's regular+playoff union.
    return df.filter(pl.col("game_type").is_in(["R", "P"])).sort(["game_date", "game_id"])


def completed_game_ids(season: int, *, session: object | None = None) -> list[int]:
    """Game ids for the season's completed (``game_state == 'OFF'``) games — scraper input."""
    sched = nhl_schedule(season, session=session)
    return sched.filter(pl.col("game_state") == "OFF")["game_id"].to_list()
