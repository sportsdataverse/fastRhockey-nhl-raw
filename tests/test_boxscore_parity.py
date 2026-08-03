"""Hermetic parity: Python boxscore parse vs R's ``final.json`` boxscore blocks.

Feeds the stored ``boxscore_raw`` (the same payload R's ``nhl_game_boxscore`` fetched)
and asserts the parsed ``team_box`` / ``skater_stats`` / ``goalie_stats`` reproduce
``final.json``'s ``team_box_parsed`` / ``skater_stats`` / ``goalie_stats``.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from nhl_raw.boxscore import parse_boxscore

FIX = Path(__file__).parent / "fixtures" / "nhl_raw"


def _load(gid: int) -> tuple[dict, dict]:
    raw = json.loads((FIX / f"raw_{gid}.json").read_text(encoding="utf-8"))
    final = json.loads((FIX / f"final_{gid}.json").read_text(encoding="utf-8"))
    return raw, final


def _cmp(py: pl.DataFrame, oracle_rows: list[dict], sort_key: str, cols: list[str]) -> None:
    o = pl.DataFrame(oracle_rows, infer_schema_length=None)
    p = py.sort(sort_key).select(cols)
    o = o.sort(sort_key).select(cols)
    assert p.height == o.height, f"row count {p.height} vs {o.height}"
    for c in cols:
        a, b = p[c].to_list(), o[c].to_list()
        if p.schema[c] in (pl.Float64, pl.Float32):
            for x, y in zip(a, b):
                assert (x is None and y is None) or abs(float(x) - float(y)) < 1e-4, f"{c}: {x} vs {y}"
        else:
            assert a == b, f"{c}: {a[:3]} vs {b[:3]}"


def test_skater_stats_parity_2024020001() -> None:
    raw, final = _load(2024020001)
    box = parse_boxscore(raw["boxscore_raw"])
    _cmp(
        box["skater_stats"],
        final["skater_stats"],
        "player_id",
        [
            "player_id",
            "team_abbrev",
            "goals",
            "assists",
            "points",
            "plus_minus",
            "shots_on_goal",
            "toi",
            "blocked_shots",
            "shifts",
        ],
    )


def test_goalie_stats_parity_2024020001() -> None:
    raw, final = _load(2024020001)
    box = parse_boxscore(raw["boxscore_raw"])
    _cmp(
        box["goalie_stats"],
        final["goalie_stats"],
        "player_id",
        ["player_id", "team_abbrev", "saves", "shots_against", "goals_against", "save_pctg", "decision", "toi"],
    )


def test_team_box_parity_2024020001() -> None:
    raw, final = _load(2024020001)
    box = parse_boxscore(raw["boxscore_raw"])
    _cmp(
        box["team_box"],
        final["team_box_parsed"],
        "team_id",
        [
            "team_id",
            "team_abbrev",
            "goals",
            "shots_on_goal",
            "pim",
            "hits",
            "blocked_shots",
            "power_play_goals",
            "faceoff_win_pctg",
            "saves",
            "save_pctg",
            "goals_against",
        ],
    )
