"""The Python schedule file must keep the R-written shape (2011-2026): the same 19
columns, int32 integers, plain (not large) strings, and R's playoff context values.
Expected values below are copied from the R-built ``nhl_schedule_2026.parquet`` /
``nhl_schedule_2020.parquet``."""

from __future__ import annotations

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from nhl_raw.schedule import _SCHEDULE_SCHEMA, _playoff_context_exprs, write_schedule_parquet

R_SCHEMA = [
    ("game_id", pa.int32()),
    ("season_full", pa.string()),
    ("game_type", pa.string()),
    ("game_date", pa.string()),
    ("game_time", pa.string()),
    ("home_team_abbr", pa.string()),
    ("away_team_abbr", pa.string()),
    ("home_team_name", pa.string()),
    ("away_team_name", pa.string()),
    ("home_score", pa.int32()),
    ("away_score", pa.int32()),
    ("game_state", pa.string()),
    ("venue", pa.string()),
    ("series_letter", pa.string()),
    ("playoff_round", pa.int32()),
    ("series_game_number", pa.int32()),
    ("season", pa.int32()),
    ("game_json", pa.bool_()),
    ("game_json_url", pa.string()),
]

# (game_id, game_type) -> R's (series_letter, playoff_round, series_game_number)
R_PLAYOFF = {
    (2025020001, "R"): (None, None, None),
    (2025030131, "P"): ("C", 1, 1),
    (2025030223, "P"): ("J", 2, 3),
    (2025030311, "P"): ("M", 3, 1),
    (2025030415, "P"): ("O", 4, 5),
    (2019030031, "P"): ("T", 0, 1),  # 2020 bubble qualifying round
}


def _frame() -> pl.DataFrame:
    rows = [
        {k: None for k in _SCHEDULE_SCHEMA} | {"game_id": g, "game_type": t, "game_state": "OFF"} for g, t in R_PLAYOFF
    ]
    return pl.DataFrame(rows, schema=_SCHEDULE_SCHEMA).with_columns(_playoff_context_exprs())


def test_playoff_context_matches_r() -> None:
    got = _frame()
    for row in got.iter_rows(named=True):
        want = R_PLAYOFF[(row["game_id"], row["game_type"])]
        assert (row["series_letter"], row["playoff_round"], row["series_game_number"]) == want, row["game_id"]


def test_written_schedule_has_r_schema(tmp_path) -> None:
    path = tmp_path / "nhl_schedule_2026.parquet"
    write_schedule_parquet(_frame(), path, 2026, {2025030131})
    t = pq.read_table(path)
    assert [(f.name, f.type) for f in t.schema] == R_SCHEMA
    rows = {r["game_id"]: r for r in t.to_pylist()}
    assert rows[2025030131]["game_json"] is True
    assert rows[2025030131]["game_json_url"] == (
        "https://raw.githubusercontent.com/sportsdataverse/fastRhockey-nhl-raw/main/nhl/json/final/2025030131.json"
    )
    assert rows[2025020001]["game_json"] is False and rows[2025020001]["game_json_url"] is None
    assert rows[2025020001]["season"] == 2026
