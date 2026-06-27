"""Faithful Python port of ``build_raw_json`` (the raw-key assembly).

Canonical R source: ``fastRhockey-nhl-raw/R/scrape_nhl_raw.R`` — ``build_raw_json``.
Reshapes the four raw api-web responses (play-by-play / boxscore / landing /
right-rail) + shifts into the old-format keys the ``fastRhockey-nhl-data`` reshaper
consumes (``linescore`` / ``decisions`` / ``scratches`` / ``officials`` / ``scoring`` /
``penalties`` / ``shots_by_period`` / ``shootout`` / ``team_coaches`` / ``game_info`` …).

Note: R branches on ``is.data.frame`` because ``jsonlite::fromJSON`` materializes some
nested arrays as data frames; Python's ``json`` always yields nested dicts/lists, so the
data-frame path collapses to the list path.
"""
from __future__ import annotations


def _extract_default(x: object) -> object:
    """Pull the ``default`` localized name; mirror of ``.extract_default``."""
    if x is None:
        return None
    if isinstance(x, dict) and x.get("default") is not None:
        return x.get("default")
    return x


def _pluck(d: object, *path: str) -> object:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _linescore(rail: dict, landing: dict) -> dict | None:
    ls = _pluck(rail, "linescore")
    if ls is None:
        return None
    out = dict(ls)
    out["shotsByPeriod"] = _pluck(rail, "shotsByPeriod")
    out["teamGameStats"] = _pluck(rail, "teamGameStats")
    out["clock"] = _pluck(landing, "clock")
    out["periodDescriptor"] = _pluck(landing, "periodDescriptor")
    out["teams"] = {
        "home": {
            "team": {
                "id": _pluck(landing, "homeTeam", "id"),
                "name": _extract_default(_pluck(landing, "homeTeam", "commonName")),
                "abbreviation": _pluck(landing, "homeTeam", "abbrev"),
            },
            "goals": _pluck(landing, "homeTeam", "score"),
            "shotsOnGoal": _pluck(landing, "homeTeam", "sog"),
        },
        "away": {
            "team": {
                "id": _pluck(landing, "awayTeam", "id"),
                "name": _extract_default(_pluck(landing, "awayTeam", "commonName")),
                "abbreviation": _pluck(landing, "awayTeam", "abbrev"),
            },
            "goals": _pluck(landing, "awayTeam", "score"),
            "shotsOnGoal": _pluck(landing, "awayTeam", "sog"),
        },
    }
    return out


def _decisions(landing: dict, box_raw: dict) -> dict | None:
    stars = _pluck(landing, "summary", "threeStars")
    if stars is None:
        return None
    winner = {"id": None, "name": None}
    loser = {"id": None, "name": None}
    for side in ("awayTeam", "homeTeam"):
        goalies = _pluck(box_raw, "playerByGameStats", side, "goalies") or []
        for gl in goalies:
            dec = gl.get("decision")
            if dec is None:
                continue
            entry = {"id": gl.get("playerId"), "name": _extract_default(gl.get("name"))}
            if dec == "W":
                winner = entry
            elif dec == "L":
                loser = entry
    return {"threeStars": stars, "winner": winner, "loser": loser}


def _scratches(rail: dict) -> list | None:
    out = []
    for side in ("awayTeam", "homeTeam"):
        for s in _pluck(rail, "gameInfo", side, "scratches") or []:
            out.append({
                "id": s.get("id"),
                "firstName": _extract_default(s.get("firstName")),
                "lastName": _extract_default(s.get("lastName")),
            })
    return out or None


def _team_coaches(rail: dict) -> dict | None:
    # R's purrr::imap over a NAMED list -> a JSON object keyed by awayTeam/homeTeam
    # (not an array), so jsonlite emits {"awayTeam": {...}, "homeTeam": {...}}.
    out = {}
    for side_key, side_label in (("awayTeam", "Away"), ("homeTeam", "Home")):
        hc = _pluck(rail, "gameInfo", side_key, "headCoach")
        if hc is None:
            continue
        out[side_key] = {"name": _extract_default(hc), "home_away": side_label}
    return out or None


def _officials(rail: dict) -> list | None:
    out = []
    for arr, role in ((_pluck(rail, "gameInfo", "referees"), "referee"),
                      (_pluck(rail, "gameInfo", "linesmen"), "linesman")):
        for o in arr or []:
            out.append({"role": role, "name": _extract_default(o)})
    return out or None


def assemble_raw(pbp_raw: dict, box_raw: dict, landing: dict, rail: dict, shifts: list | None) -> dict | None:
    """Port of ``build_raw_json`` assembly — the four responses -> old-format keys."""
    if pbp_raw is None:
        return None
    game_info = {
        "id": _pluck(pbp_raw, "id"), "season": _pluck(pbp_raw, "season"),
        "gameType": _pluck(pbp_raw, "gameType"), "gameDate": _pluck(pbp_raw, "gameDate"),
        "venue": _pluck(pbp_raw, "venue"), "gameState": _pluck(pbp_raw, "gameState"),
        "startTimeUTC": _pluck(pbp_raw, "startTimeUTC"), "homeTeam": _pluck(pbp_raw, "homeTeam"),
        "awayTeam": _pluck(pbp_raw, "awayTeam"),
    }
    shootout = _pluck(landing, "summary", "shootout") or None
    sbp = _pluck(rail, "shotsByPeriod") or None
    return {
        "all_plays": _pluck(pbp_raw, "plays"),
        "game_info": game_info,
        "rosters": _pluck(pbp_raw, "rosterSpots"),
        "team_box": _pluck(rail, "teamGameStats"),
        "player_box": _pluck(box_raw, "playerByGameStats"),
        "linescore": _linescore(rail, landing),
        "decisions": _decisions(landing, box_raw),
        "scratches": _scratches(rail),
        "team_coaches": _team_coaches(rail),
        "scoring": _pluck(landing, "summary", "scoring"),
        "penalties": _pluck(landing, "summary", "penalties"),
        "officials": _officials(rail),
        "shots_by_period": sbp,
        "shootout": shootout,
        "shifts": shifts,
        "pbp_raw": pbp_raw,
        "boxscore_raw": box_raw,
        "landing_raw": landing,
        "right_rail_raw": rail,
    }
