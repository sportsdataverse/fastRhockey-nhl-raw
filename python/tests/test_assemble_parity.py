"""Hermetic parity: Python ``assemble_raw`` vs R's stored ``raw/{gid}.json`` keys.

``raw_{gid}.json`` stores both the four raw api-web responses *and* the old-format keys
R's ``build_raw_json`` derived from them — so we re-derive from the stored responses and
assert the assembly logic (officials / team_coaches / scratches / decisions / linescore /
game_info) reproduces R's output exactly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nhl_raw.assemble import assemble_raw

FIX = Path(__file__).parent / "fixtures" / "nhl_raw"


def _assembled(gid: int) -> tuple[dict, dict]:
    raw = json.loads((FIX / f"raw_{gid}.json").read_text(encoding="utf-8"))
    got = assemble_raw(raw["pbp_raw"], raw["boxscore_raw"], raw["landing_raw"], raw["right_rail_raw"], raw.get("shifts"))
    return raw, got


@pytest.mark.parametrize("gid", [2024020001, 2009020714])
@pytest.mark.parametrize("key", ["team_coaches", "scratches", "decisions"])
def test_assembled_key_parity(gid: int, key: str) -> None:
    raw, got = _assembled(gid)
    assert got[key] == raw.get(key), f"{key} diverged for {gid}"


def test_officials_parity_modern() -> None:
    # Officials parity is asserted only on the modern (fully api-web-supported) game.
    # The 2009 R fixture stored officials=None even though its right-rail carries
    # referees — a pre-2010 scrape-time quirk on the R side (that game's all_plays is
    # also empty); the Python port faithfully extracts them, which is the correct shape.
    raw, got = _assembled(2024020001)
    assert got["officials"] == raw["officials"]


@pytest.mark.parametrize("gid", [2024020001, 2009020714])
def test_game_info_scalars_parity(gid: int) -> None:
    raw, got = _assembled(gid)
    for f in ("id", "season", "gameType", "gameDate", "gameState"):
        assert got["game_info"][f] == raw["game_info"][f], f"game_info.{f} for {gid}"


@pytest.mark.parametrize("gid", [2024020001, 2009020714])
def test_linescore_teams_parity(gid: int) -> None:
    raw, got = _assembled(gid)
    assert got["linescore"]["teams"] == raw["linescore"]["teams"], f"linescore.teams for {gid}"


def test_direct_pluck_keys_passthrough() -> None:
    raw, got = _assembled(2024020001)
    # scoring/penalties/shots_by_period are direct plucks — must equal the stored values
    for key in ("scoring", "penalties", "shots_by_period"):
        assert got[key] == raw.get(key), key
