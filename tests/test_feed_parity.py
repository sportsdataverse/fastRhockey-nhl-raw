"""Hermetic parity: Python PBP enrichment vs the R-produced ``final.all_plays``.

The fixtures' ``raw_{gid}.json`` stores the four raw api-web responses (``pbp_raw``
etc.) and ``final_{gid}.json`` stores R's enriched output — so we feed the *same*
``pbp_raw`` the R ``nhl_game_feed`` saw and assert the Python port reproduces it,
with no live fetch. Provenance: see ``tests/fixtures/nhl_raw/README.md``.

SP-B asserts the shift-independent geometry (coords / shot_distance / shot_angle)
on shot events; on-ice / strength / descriptions / xG land in SP-C+.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from nhl_raw.feed import build_pbp

FIX = Path(__file__).parent / "fixtures" / "nhl_raw"
GID = 2024020001
_FENWICK = ["SHOT", "GOAL", "MISSED_SHOT", "BLOCKED_SHOT"]
_KEY = ["event_type", "period", "period_seconds", "event_player_1_id", "x", "y"]
_GEOMETRY = ["x_fixed", "y_fixed", "shot_distance", "shot_angle"]


def _load(gid: int) -> tuple[dict, dict]:
    raw = json.loads((FIX / f"raw_{gid}.json").read_text(encoding="utf-8"))
    final = json.loads((FIX / f"final_{gid}.json").read_text(encoding="utf-8"))
    return raw, final


def _norm(df: pl.DataFrame) -> pl.DataFrame:
    """Cast the join keys + asserted columns to a common dtype so the keyed join lines up."""
    return df.with_columns(
        pl.col("event_type").cast(pl.Utf8),
        pl.col("period").cast(pl.Int64),
        pl.col("period_seconds").cast(pl.Int64),
        pl.col("event_player_1_id").cast(pl.Int64),
        pl.col("x").cast(pl.Float64),
        pl.col("y").cast(pl.Float64),
        *[pl.col(c).cast(pl.Float64) for c in _GEOMETRY],
    )


def test_shot_geometry_parity_2024020001() -> None:
    raw, final = _load(GID)
    py = _norm(build_pbp(raw["pbp_raw"], GID).filter(pl.col("event_type").is_in(_FENWICK)).select(_KEY + _GEOMETRY))
    # Project final.all_plays to just the asserted keys before building the frame — the
    # full 93-col tibble has mixed-type columns (e.g. reason='tv-timeout') that break
    # schema inference.
    cols = _KEY + _GEOMETRY
    rows = [{k: p.get(k) for k in cols} for p in final["all_plays"] if p.get("event_type") in _FENWICK]
    oracle = _norm(pl.DataFrame(rows, infer_schema_length=None))

    # Keyed join (robust to the shift-integration reordering that happens later).
    matched = py.drop_nulls("event_player_1_id").join(oracle, on=_KEY, how="inner", suffix="_o")
    keyed = py.drop_nulls("event_player_1_id").height
    assert matched.height >= 0.9 * keyed, f"only {matched.height}/{keyed} shot events matched the oracle"

    for col in _GEOMETRY:
        diff = (matched[col] - matched[f"{col}_o"]).abs().max()
        assert diff is not None and diff < 0.11, f"{col}: max abs diff {diff} vs oracle (rounding tol 0.1)"


def test_parse_is_deterministic_and_nonempty() -> None:
    raw, _ = _load(GID)
    a = build_pbp(raw["pbp_raw"], GID)
    b = build_pbp(raw["pbp_raw"], GID)
    assert a.height > 0 and a.equals(b)


# ----- SP-C: shift integration (on-ice) + strength states -----


def _onice_sig(p: dict) -> tuple:
    """Order-independent on-ice signature for one event row (robust to fill order)."""
    home = tuple(sorted(x for x in (p.get(f"home_on_{i}_id") for i in range(1, 8)) if x is not None))
    away = tuple(sorted(x for x in (p.get(f"away_on_{i}_id") for i in range(1, 8)) if x is not None))
    return (home, away, p.get("home_goalie_id"), p.get("away_goalie_id"))


def test_onice_strength_parity_2024020001() -> None:
    raw, final = _load(GID)
    py = build_pbp(raw["pbp_raw"], GID, shifts=raw["shifts"]).to_dicts()

    # Row count now includes the interleaved CHANGE rows.
    assert len(py) == len(final["all_plays"]), f"{len(py)} vs oracle {len(final['all_plays'])}"

    def key(p: dict) -> tuple:
        return (p["event_type"], p["period"], p["period_seconds"], p.get("event_player_1_id"))

    oracle = {
        key(p): p for p in final["all_plays"] if p["event_type"] != "CHANGE" and p.get("event_player_1_id") is not None
    }

    checked = onice_ok = ss_ok = sc_ok = 0
    for p in py:
        if p["event_type"] == "CHANGE" or p.get("event_player_1_id") is None:
            continue
        o = oracle.get(key(p))
        if o is None:
            continue
        checked += 1
        onice_ok += _onice_sig(p) == _onice_sig(o)
        ss_ok += p.get("strength_state") == o.get("strength_state")
        sc_ok += p.get("strength_code") == o.get("strength_code")

    assert checked >= 200, f"only {checked} events matched for comparison"
    assert onice_ok / checked >= 0.97, f"on-ice set parity {onice_ok}/{checked}"
    assert ss_ok / checked >= 0.99, f"strength_state parity {ss_ok}/{checked}"
    # strength_code is order-sensitive (lead/lag nulling near CHANGE boundaries).
    assert sc_ok / checked >= 0.95, f"strength_code parity {sc_ok}/{checked}"


# ----- SP-D1: event-player names + descriptions + finalize -----


def test_descriptions_parity_2024020001() -> None:
    raw, final = _load(GID)
    py = build_pbp(raw["pbp_raw"], GID, shifts=raw["shifts"]).to_dicts()

    def key(p: dict) -> tuple:
        return (p["event_type"], p["period"], p["period_seconds"], p.get("event_player_1_id"))

    oracle = {
        key(p): p for p in final["all_plays"] if p["event_type"] != "CHANGE" and p.get("event_player_1_id") is not None
    }
    checked = desc_ok = name_ok = 0
    for p in py:
        if p["event_type"] == "CHANGE" or p.get("event_player_1_id") is None:
            continue
        o = oracle.get(key(p))
        if o is None:
            continue
        checked += 1
        desc_ok += p.get("description") == o.get("description")
        name_ok += p.get("event_player_1_name") == o.get("event_player_1_name")

    assert checked >= 200, f"only {checked} events matched"
    assert name_ok / checked >= 0.99, f"event_player_1_name parity {name_ok}/{checked}"
    assert desc_ok / checked >= 0.98, f"description parity {desc_ok}/{checked}"


def test_event_idx_is_sequential() -> None:
    raw, _ = _load(GID)
    py = build_pbp(raw["pbp_raw"], GID, shifts=raw["shifts"])
    assert py["event_idx"].to_list() == list(range(py.height))


# ----- SP-D2: expected goals (xG) -----

MODELS = Path(__file__).parent / "fixtures" / "models"


def test_xg_parity_2024020001() -> None:
    from nhl_raw.xg import load_xg_models

    raw, final = _load(GID)
    models = load_xg_models(MODELS)
    py = build_pbp(raw["pbp_raw"], GID, shifts=raw["shifts"], xg=models).to_dicts()

    def key(p: dict) -> tuple:
        return (p["event_type"], p["period"], p["period_seconds"], p.get("event_player_1_id"))

    shots = ["SHOT", "GOAL", "MISSED_SHOT"]
    oracle = {
        key(p): p for p in final["all_plays"] if p["event_type"] in shots and p.get("event_player_1_id") is not None
    }

    checked = ok = 0
    worst = 0.0
    for p in py:
        if p["event_type"] not in shots or p.get("event_player_1_id") is None:
            continue
        o = oracle.get(key(p))
        if o is None:
            continue
        checked += 1
        pxg, oxg = p.get("xg"), o.get("xg")
        if pxg is None and oxg is None:
            ok += 1
        elif pxg is not None and oxg is not None:
            worst = max(worst, abs(pxg - oxg))
            ok += abs(pxg - oxg) < 0.005  # final.json xG is 4-dp jsonlite-rounded

    assert checked >= 80, f"only {checked} shots matched"
    # both the null pattern (invalid last-event shots) and the predicted values must agree
    assert ok / checked >= 0.98, f"xG parity {ok}/{checked} (worst abs diff {worst:.5f})"


def test_ensure_xg_models_uses_local_without_network() -> None:
    # An explicit dir whose 3 files exist (the fixtures) must resolve offline — no download.
    from nhl_raw.xg import ensure_xg_models

    assert ensure_xg_models(MODELS) == MODELS
    for fn in ("xg_model_5v5.json", "xg_model_st.json", "xg_model_meta.json"):
        assert (MODELS / fn).exists()


def test_default_model_dir_env_override(monkeypatch, tmp_path) -> None:
    from nhl_raw.xg import default_model_dir

    monkeypatch.setenv("NHL_RAW_MODEL_DIR", str(tmp_path / "m"))
    assert default_model_dir() == tmp_path / "m"
    monkeypatch.delenv("NHL_RAW_MODEL_DIR", raising=False)
    assert default_model_dir().parts[-2:] == ("nhl_raw", "xg_models")
