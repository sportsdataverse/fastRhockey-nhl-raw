"""Unspaced ``name.default`` values ("J.Kulich") come out as "J. Kulich".

Fixture: the trimmed real ``boxscore_raw`` of game 2026020305 (see the fixtures README).
"""

from __future__ import annotations

import json
from pathlib import Path

from nhl_raw.boxscore import _player_name, parse_boxscore

FIX = Path(__file__).parent / "fixtures" / "nhl_raw"


def test_unspaced_player_name_is_spaced_2026020305() -> None:
    raw = json.loads((FIX / "boxscore_2026020305.json").read_text(encoding="utf-8"))
    box = parse_boxscore(raw)
    names = box["skater_stats"]["player_name"].to_list() + box["goalie_stats"]["player_name"].to_list()
    assert "J. Kulich" in names
    assert "J.Kulich" not in names
    assert "Z. Benson" in names
    assert all(" " in n for n in names)


def test_player_name_leaves_spaced_and_non_string_names() -> None:
    assert _player_name({"default": "J.T. Compher"}) == "J.T. Compher"
    assert _player_name({"default": "J.T.Compher"}) == "J.T. Compher"
    assert _player_name({"default": "E. Del Mastro"}) == "E. Del Mastro"
    assert _player_name({"default": "D.Jiricek"}) == "D. Jiricek"
    assert _player_name(None) is None
