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


def test_rebuild_final_from_raw_writes_the_same_final_offline(tmp_path, monkeypatch) -> None:
    """--from-raw rebuilds final/{gid}.json from raw/{gid}.json alone: no fetch may run."""
    from nhl_raw import scrape

    def no_network(*a, **k):
        raise AssertionError("rebuild-from-raw touched the network")

    monkeypatch.setattr(scrape, "fetch_endpoint", no_network)
    monkeypatch.setattr(scrape, "nhl_game_shifts", no_network)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{GID}.json").write_bytes((FIX / f"raw_{GID}.json").read_bytes())
    xg = load_xg_models(MODELS)
    summary = scrape.rebuild_season(2025, out_dir=tmp_path, xg=xg)
    assert summary == {"season": 2025, "to_rebuild": 1, "rebuilt": 1, "failed": 0, "absent": 0}
    raw = json.loads((FIX / f"raw_{GID}.json").read_text(encoding="utf-8"))
    expected = build_final_from_responses(
        raw["pbp_raw"], raw["boxscore_raw"], raw["landing_raw"], raw["right_rail_raw"], raw.get("shifts"), GID, xg=xg
    )
    written = json.loads((tmp_path / "final" / f"{GID}.json").read_text(encoding="utf-8"))
    assert written == json.loads(json.dumps(expected))
    assert any(p.get("xg") for p in written["all_plays"])


def test_rebuild_season_counts_a_missing_raw_as_absent(tmp_path) -> None:
    from nhl_raw import scrape

    assert scrape.rebuild_final_from_raw(GID, out_dir=tmp_path) is False
    assert scrape.rebuild_season(2025, out_dir=tmp_path)["to_rebuild"] == 0
