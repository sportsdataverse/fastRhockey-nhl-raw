"""Python port of the fastRhockey NHL raw scraper + enrichment pipeline.

Reproduces ``fastRhockey-nhl-raw``'s ``raw/{game_id}.json`` and the enriched
``final/{game_id}.json`` (the input the ``fastRhockey-nhl-data`` reshaper consumes)
without R, so the NHL data pipeline can run self-sufficiently in Python.

Top-level entry points::

    from nhl_raw import build_final_json, download_game
    from nhl_raw.xg import load_xg_models
    final = build_final_json(2024020001, xg=load_xg_models("path/to/models"))
"""

from nhl_raw.assemble import assemble_raw
from nhl_raw.boxscore import parse_boxscore
from nhl_raw.feed import (
    add_shot_metrics,
    add_strength_states,
    build_onice_matrix,
    build_pbp,
    fix_coordinates,
    integrate_shifts,
    parse_game_rosters,
    parse_plays,
)
from nhl_raw.scrape import build_final_from_responses, build_final_json, build_raw_json, download_game
from nhl_raw.shifts import nhl_game_shifts

__all__ = [
    # enrichment
    "build_pbp",
    "parse_plays",
    "fix_coordinates",
    "add_shot_metrics",
    "parse_game_rosters",
    "integrate_shifts",
    "build_onice_matrix",
    "add_strength_states",
    # boxscore + assembly + shifts
    "parse_boxscore",
    "assemble_raw",
    "nhl_game_shifts",
    # scraper driver
    "build_final_json",
    "build_raw_json",
    "download_game",
    "build_final_from_responses",
]
