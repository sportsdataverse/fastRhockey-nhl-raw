"""Hermetic regression for the scrape wiring (``build_final_from_responses``).

Feeds the stored raw responses (no network) and asserts the final-JSON assembly overlays
the enriched PBP + parsed game_info/rosters/boxscore the way ``build_final_json`` does.
The live end-to-end path (``build_final_json`` against api-web) is exercised manually —
see the commit message / README — since it requires network.
"""

from __future__ import annotations

import json
from pathlib import Path

from nhl_raw.scrape import build_final_from_responses
from nhl_raw.xg import load_xg_models

FIX = Path(__file__).parent / "fixtures" / "nhl_raw"
MODELS = Path(__file__).parent / "fixtures" / "models"
GID = 2024020001


def test_build_final_from_responses_parity() -> None:
    raw = json.loads((FIX / f"raw_{GID}.json").read_text(encoding="utf-8"))
    oracle = json.loads((FIX / f"final_{GID}.json").read_text(encoding="utf-8"))
    final = build_final_from_responses(
        raw["pbp_raw"],
        raw["boxscore_raw"],
        raw["landing_raw"],
        raw["right_rail_raw"],
        raw.get("shifts"),
        GID,
        xg=load_xg_models(MODELS),
    )
    assert len(final["all_plays"]) == len(oracle["all_plays"])
    assert len(final["skater_stats"]) == len(oracle["skater_stats"])
    assert len(final["goalie_stats"]) == len(oracle["goalie_stats"])
    assert len(final["team_box_parsed"]) == 2

    # game_info is overlaid with the *parsed* one-row version (not the raw assembled dict).
    gi = final["game_info"][0]
    assert gi["game_id"] == GID and gi["game_type"] == "R"
    assert {"home_team_abbr", "away_team_abbr", "game_state", "venue"} <= set(gi)

    # the enriched all_plays carries xG on shots
    goals = [p for p in final["all_plays"] if p["event_type"] == "GOAL"]
    assert goals and goals[0].get("xg") is not None
