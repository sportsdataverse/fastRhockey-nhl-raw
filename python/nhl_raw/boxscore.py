"""Faithful Python port of fastRhockey's NHL boxscore parsing.

Canonical R source: ``fastRhockey/R/nhl_game_boxscore.R`` — ``nhl_game_boxscore`` →
``.boxscore_game_info`` / ``.boxscore_team_box`` / ``.boxscore_skater_stats`` /
``.boxscore_goalie_stats``. Reproduces the ``team_box_parsed`` / ``skater_stats`` /
``goalie_stats`` blocks that ``build_final_json`` writes into ``final/{game_id}.json``.
"""

from __future__ import annotations

import polars as pl

_SKATER_SCHEMA = {
    "home_away": pl.Utf8,
    "team_id": pl.Int64,
    "team_abbrev": pl.Utf8,
    "player_id": pl.Int64,
    "player_name": pl.Utf8,
    "sweater_number": pl.Int64,
    "position": pl.Utf8,
    "goals": pl.Int64,
    "assists": pl.Int64,
    "points": pl.Int64,
    "plus_minus": pl.Int64,
    "pim": pl.Int64,
    "hits": pl.Int64,
    "power_play_goals": pl.Int64,
    "shots_on_goal": pl.Int64,
    "faceoff_winning_pctg": pl.Float64,
    "toi": pl.Utf8,
    "blocked_shots": pl.Int64,
    "shifts": pl.Int64,
    "giveaways": pl.Int64,
    "takeaways": pl.Int64,
}
_GOALIE_SCHEMA = {
    "home_away": pl.Utf8,
    "team_id": pl.Int64,
    "team_abbrev": pl.Utf8,
    "player_id": pl.Int64,
    "player_name": pl.Utf8,
    "sweater_number": pl.Int64,
    "even_strength_shots_against": pl.Utf8,
    "power_play_shots_against": pl.Utf8,
    "shorthanded_shots_against": pl.Utf8,
    "save_shots_against": pl.Utf8,
    "save_pctg": pl.Float64,
    "even_strength_goals_against": pl.Int64,
    "power_play_goals_against": pl.Int64,
    "shorthanded_goals_against": pl.Int64,
    "pim": pl.Int64,
    "goals_against": pl.Int64,
    "toi": pl.Utf8,
    "starter": pl.Boolean,
    "decision": pl.Utf8,
    "shots_against": pl.Int64,
    "saves": pl.Int64,
}
_TEAM_SCHEMA = {
    "home_away": pl.Utf8,
    "team_id": pl.Int64,
    "team_abbrev": pl.Utf8,
    "team_name": pl.Utf8,
    "goals": pl.Int64,
    "shots_on_goal": pl.Int64,
    "pim": pl.Int64,
    "hits": pl.Int64,
    "blocked_shots": pl.Int64,
    "giveaways": pl.Int64,
    "takeaways": pl.Int64,
    "power_play_goals": pl.Int64,
    "faceoff_win_pctg": pl.Float64,
    "saves": pl.Int64,
    "save_pctg": pl.Float64,
    "goals_against": pl.Int64,
}


def _int(v: object) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _num(v: object) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _default_name(v: object) -> str | None:
    return v.get("default") if isinstance(v, dict) else v


def _skaters(team_pbs: dict) -> list[dict]:
    return list(team_pbs.get("forwards") or []) + list(team_pbs.get("defense") or [])


def _sum(rows: list[dict], key: str) -> int | None:
    vals = [_int(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else (0 if rows else None)


def parse_game_info(data: dict) -> pl.DataFrame:
    away, home = data.get("awayTeam") or {}, data.get("homeTeam") or {}
    return pl.DataFrame(
        [
            {
                "game_id": _int(data.get("id")),
                "season": _int(data.get("season")),
                "game_type": _int(data.get("gameType")),
                "game_date": data.get("gameDate"),
                "venue": _default_name(data.get("venue")),
                "game_state": data.get("gameState"),
                "away_team_id": _int(away.get("id")),
                "away_team_abbrev": away.get("abbrev"),
                "away_team_name": _default_name(away.get("commonName")),
                "away_score": _int(away.get("score")),
                "away_sog": _int(away.get("sog")),
                "home_team_id": _int(home.get("id")),
                "home_team_abbrev": home.get("abbrev"),
                "home_team_name": _default_name(home.get("commonName")),
                "home_score": _int(home.get("score")),
                "home_sog": _int(home.get("sog")),
                "last_period_type": (data.get("gameOutcome") or {}).get("lastPeriodType"),
            }
        ]
    )


def parse_skater_stats(data: dict) -> pl.DataFrame:
    pbs = data.get("playerByGameStats") or {}
    rows = []
    for side, tk in (("away", "awayTeam"), ("home", "homeTeam")):
        team = data.get(tk) or {}
        for sk in _skaters(pbs.get(tk) or {}):
            rows.append(
                {
                    "home_away": side,
                    "team_id": _int(team.get("id")),
                    "team_abbrev": team.get("abbrev"),
                    "player_id": _int(sk.get("playerId")),
                    "player_name": _default_name(sk.get("name")),
                    "sweater_number": _int(sk.get("sweaterNumber")),
                    "position": sk.get("position"),
                    "goals": _int(sk.get("goals")),
                    "assists": _int(sk.get("assists")),
                    "points": _int(sk.get("points")),
                    "plus_minus": _int(sk.get("plusMinus")),
                    "pim": _int(sk.get("pim")),
                    "hits": _int(sk.get("hits")),
                    "power_play_goals": _int(sk.get("powerPlayGoals")),
                    "shots_on_goal": _int(sk.get("sog")),
                    "faceoff_winning_pctg": _num(sk.get("faceoffWinningPctg")),
                    "toi": sk.get("toi"),
                    "blocked_shots": _int(sk.get("blockedShots")),
                    "shifts": _int(sk.get("shifts")),
                    "giveaways": _int(sk.get("giveaways")),
                    "takeaways": _int(sk.get("takeaways")),
                }
            )
    return pl.DataFrame(rows, schema=_SKATER_SCHEMA)


def parse_goalie_stats(data: dict) -> pl.DataFrame:
    pbs = data.get("playerByGameStats") or {}
    rows = []
    for side, tk in (("away", "awayTeam"), ("home", "homeTeam")):
        team = data.get(tk) or {}
        for gl in (pbs.get(tk) or {}).get("goalies") or []:
            rows.append(
                {
                    "home_away": side,
                    "team_id": _int(team.get("id")),
                    "team_abbrev": team.get("abbrev"),
                    "player_id": _int(gl.get("playerId")),
                    "player_name": _default_name(gl.get("name")),
                    "sweater_number": _int(gl.get("sweaterNumber")),
                    "even_strength_shots_against": gl.get("evenStrengthShotsAgainst"),
                    "power_play_shots_against": gl.get("powerPlayShotsAgainst"),
                    "shorthanded_shots_against": gl.get("shorthandedShotsAgainst"),
                    "save_shots_against": gl.get("saveShotsAgainst"),
                    "save_pctg": _num(gl.get("savePctg")),
                    "even_strength_goals_against": _int(gl.get("evenStrengthGoalsAgainst")),
                    "power_play_goals_against": _int(gl.get("powerPlayGoalsAgainst")),
                    "shorthanded_goals_against": _int(gl.get("shorthandedGoalsAgainst")),
                    "pim": _int(gl.get("pim")),
                    "goals_against": _int(gl.get("goalsAgainst")),
                    "toi": gl.get("toi"),
                    "starter": gl.get("starter"),
                    "decision": gl.get("decision"),
                    "shots_against": _int(gl.get("shotsAgainst")),
                    "saves": _int(gl.get("saves")),
                }
            )
    return pl.DataFrame(rows, schema=_GOALIE_SCHEMA)


def parse_team_box(data: dict) -> pl.DataFrame:
    pbs = data.get("playerByGameStats") or {}
    rows = []
    for side, tk in (("away", "awayTeam"), ("home", "homeTeam")):
        team = data.get(tk) or {}
        sk = _skaters(pbs.get(tk) or {})
        gl = list((pbs.get(tk) or {}).get("goalies") or [])
        fo = [_num(r.get("faceoffWinningPctg")) for r in sk]
        fo = [v for v in fo if v is not None and v > 0]
        sa = _sum(gl, "shotsAgainst") or 0
        rows.append(
            {
                "home_away": side,
                "team_id": _int(team.get("id")),
                "team_abbrev": team.get("abbrev"),
                "team_name": _default_name(team.get("commonName")),
                "goals": _int(team.get("score")),
                "shots_on_goal": _int(team.get("sog")),
                "pim": _sum(sk, "pim"),
                "hits": _sum(sk, "hits"),
                "blocked_shots": _sum(sk, "blockedShots"),
                "giveaways": _sum(sk, "giveaways"),
                "takeaways": _sum(sk, "takeaways"),
                "power_play_goals": _sum(sk, "powerPlayGoals"),
                "faceoff_win_pctg": round(sum(fo) / len(fo), 4) if fo else None,
                "saves": _sum(gl, "saves"),
                "save_pctg": round((_sum(gl, "saves") or 0) / max(sa, 1), 4) if gl else None,
                "goals_against": _sum(gl, "goalsAgainst"),
            }
        )
    return pl.DataFrame(rows, schema=_TEAM_SCHEMA)


def parse_boxscore(data: dict | None) -> dict[str, pl.DataFrame]:
    """Port of ``nhl_game_boxscore`` — boxscore endpoint payload -> 4 tidy frames.

    Tolerates ``None`` (a failed/absent boxscore fetch) by returning empty frames, so a
    transient boxscore miss doesn't crash the final-JSON build mid-game (R's nhl_game_boxscore
    similarly swallows the failure and leaves the boxscore keys empty)."""
    data = data or {}
    return {
        "game_info": parse_game_info(data),
        "team_box": parse_team_box(data),
        "skater_stats": parse_skater_stats(data),
        "goalie_stats": parse_goalie_stats(data),
    }
