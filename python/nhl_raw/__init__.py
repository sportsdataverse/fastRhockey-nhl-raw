"""Python port of the fastRhockey NHL raw scraper + enrichment pipeline.

Reproduces ``fastRhockey-nhl-raw``'s ``raw/{game_id}.json`` and the enriched
``final/{game_id}.json`` (the input the ``fastRhockey-nhl-data`` reshaper consumes)
without R, so the NHL data pipeline can run self-sufficiently in Python.
"""

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

__all__ = [
    "build_pbp",
    "parse_plays",
    "fix_coordinates",
    "add_shot_metrics",
    "parse_game_rosters",
    "integrate_shifts",
    "build_onice_matrix",
    "add_strength_states",
]
