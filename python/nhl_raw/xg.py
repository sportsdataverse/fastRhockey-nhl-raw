"""Faithful Python port of fastRhockey's NHL expected-goals (xG) pipeline.

Canonical R source: ``fastRhockey/R/helpers_nhl.R`` — ``helper_nhl_prepare_xg_data``
and ``helper_nhl_calculate_xg`` (the feature recipe + three-model prediction), plus
``zzz.R``'s ``.load_xg_models`` (model loading).

Three models keyed off ``strength_state``:
  * **5v5** (``xg_model_5v5.json``, 36 feats) — even strength.
  * **special teams** (``xg_model_st.json``, 38 feats = 5v5 + ``total_skaters_on`` +
    ``event_team_advantage``) — every non-5v5 situation.
  * **penalty shot** — a constant (``xg_model_ps``, from the release manifest).

The two boosters are plain XGBoost JSON (cross-language), and carry their
``feature_names`` embedded — so they are self-describing and load natively in
Python xgboost; only the penalty-shot constant comes from ``xg_model_meta.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import numpy as np
import polars as pl

_PS_DEFAULT = 0.3202197

# Published canonical boosters (download-on-first-use). Mirrors nflverse's
# download-on-demand+cache pattern — the models are NOT bundled in the wheel.
_XG_RELEASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nhl_xg_models"
# The release's xg_model_meta.json is its manifest: ``xg_model_ps``, a ``version`` and the
# sha256 of each model file, written by fastRhockey-nhl-data's ``nhl_data_build.xg_publish``.
# A cached booster that no longer matches it is replaced, so a retrain ships by publishing
# (fastRhockey's ``.xg_manifest`` / ``.xg_file`` do the same in R).
_XG_MANIFEST = "xg_model_meta.json"
_XG_BOOSTERS = ("xg_model_5v5.json", "xg_model_st.json")

# secondary_type normalization spanning the 2010-2022 (Title Case) and 2023+ (lowercase
# abbreviated) NHL APIs -> the canonical values the xG models were trained on.
_SHOT_TYPE_NORM = {
    "wrist": "Wrist Shot",
    "wrist shot": "Wrist Shot",
    "snap": "Snap Shot",
    "snap shot": "Snap Shot",
    "slap": "Slap Shot",
    "slap shot": "Slap Shot",
    "backhand": "Backhand",
    "deflected": "Deflected",
    "tip-in": "Tip-In",
    "wrap-around": "Wrap-around",
    "bat": "Batted",
    "batted": "Batted",
    "poke": "Poke",
    "between-legs": "Between Legs",
    "between legs": "Between Legs",
    "cradle": "Cradle",
    "penalty shot": "Penalty Shot",
}
# Canonical shot type -> the janitor::clean_names() one-hot column the model expects.
_SHOT_TYPE_COL = {
    "Wrist Shot": "wrist_shot",
    "Snap Shot": "snap_shot",
    "Slap Shot": "slap_shot",
    "Backhand": "backhand",
    "Wrap-around": "wrap_around",
    "Tip-In": "tip_in",
    "Deflected": "deflected",
    "Poke": "poke",
    "Batted": "batted",
    "Between Legs": "between_legs",
    "Cradle": "cradle",
}
# Valid last-event types -> the "last_" one-hot column (clean_names of "last_<TYPE>").
_LAST_EVENT_COL = {
    "FACEOFF": "last_faceoff",
    "GIVEAWAY": "last_giveaway",
    "TAKEAWAY": "last_takeaway",
    "BLOCKED_SHOT": "last_blocked_shot",
    "HIT": "last_hit",
    "MISSED_SHOT": "last_missed_shot",
    "SHOT": "last_shot",
    "STOP": "last_stop",
    "PENALTY": "last_penalty",
    "GOAL": "last_goal",
}
_VALID_LAST = list(_LAST_EVENT_COL.keys())
_UNBLOCKED = ["SHOT", "MISSED_SHOT", "GOAL"]


def default_model_dir() -> Path:
    """Local cache dir for the downloaded boosters (override via ``NHL_RAW_MODEL_DIR``)."""
    override = os.environ.get("NHL_RAW_MODEL_DIR")
    return Path(override) if override else Path.home() / ".cache" / "nhl_raw" / "xg_models"


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _read_manifest(raw: bytes) -> dict:
    """Parse a release manifest; raise ``ValueError`` unless it pins both boosters."""
    m = json.loads(raw)
    pins = m.get("files") if isinstance(m, dict) else None
    if not (
        isinstance(pins, dict)
        and "xg_model_ps" in m
        and all(re.fullmatch(r"[0-9a-f]{64}", str(pins.get(fn, ""))) for fn in _XG_BOOSTERS)
    ):
        raise ValueError(
            f"the nhl_xg_models release manifest {_XG_MANIFEST} is not in the expected format: "
            f"it must carry xg_model_ps and a sha256 for each of {', '.join(_XG_BOOSTERS)}"
        )
    return m


def _write_atomic(d: Path, fn: str, data: bytes) -> None:
    # Its own temp file per write, so two processes loading at once never read a partial file.
    fd, tmp = tempfile.mkstemp(prefix=f"{fn}-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, d / fn)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_xg_models(model_dir: str | Path | None = None) -> Path:
    """Return a dir holding the two boosters, checked against the release manifest.

    Each call fetches the ``nhl_xg_models`` release's ``xg_model_meta.json`` manifest and
    re-downloads any cached booster whose sha256 differs from it, so a retrained release
    replaces an old cache; a download that does not match the manifest is refused. Offline,
    the cached manifest is used. An explicit ``model_dir`` that already holds both boosters
    (e.g. the test fixtures) is used as-is and never touches the network; ``None`` resolves
    to ``default_model_dir()``."""
    import requests

    if model_dir is not None:
        local = Path(model_dir)
        if all((local / fn).is_file() and (local / fn).stat().st_size > 0 for fn in _XG_BOOSTERS):
            return local
    d = Path(model_dir) if model_dir is not None else default_model_dir()
    d.mkdir(parents=True, exist_ok=True)

    def get(fn: str) -> bytes:
        r = requests.get(f"{_XG_RELEASE}/{fn}", timeout=60, headers={"User-Agent": "nhl-raw/0.0.1"})
        r.raise_for_status()
        return r.content

    cached = d / _XG_MANIFEST
    try:
        raw = get(_XG_MANIFEST)
        manifest = _read_manifest(raw)
        _write_atomic(d, _XG_MANIFEST, raw)
    except (requests.RequestException, ValueError) as exc:
        if not cached.is_file():
            raise RuntimeError(f"could not fetch the nhl_xg_models release manifest: {exc}") from exc
        manifest = _read_manifest(cached.read_bytes())  # offline: the last manifest seen
    for fn in _XG_BOOSTERS:
        pin = manifest["files"][fn]
        if _sha256(d / fn) == pin:
            continue
        data = get(fn)
        if hashlib.sha256(data).hexdigest() != pin:
            raise RuntimeError(
                f"the downloaded {fn} does not match the nhl_xg_models release manifest; "
                "the release may be mid-update, try again shortly"
            )
        _write_atomic(d, fn, data)
    return d


def load_xg_models(model_dir: str | Path | None = None) -> dict:
    """Load the two boosters (+ embedded feature names) and the penalty-shot constant.

    ``model_dir=None`` uses the canonical models from the ``nhl_xg_models`` release, cached
    under ``default_model_dir()`` and checked against the release manifest on every load (see
    ``ensure_xg_models``); pass a dir to use local models."""
    import xgboost as xgb

    d = ensure_xg_models(model_dir)
    b5, bst = xgb.Booster(), xgb.Booster()
    b5.load_model(str(d / "xg_model_5v5.json"))
    bst.load_model(str(d / "xg_model_st.json"))
    meta = d / "xg_model_meta.json"
    ps = json.loads(meta.read_text()).get("xg_model_ps", _PS_DEFAULT) if meta.exists() else _PS_DEFAULT
    return {"m5v5": b5, "mst": bst, "feats_5v5": b5.feature_names, "feats_st": bst.feature_names, "ps": ps}


def _norm_secondary_type() -> pl.Expr:
    e = pl.col("secondary_type")
    expr = pl.when(e.is_null()).then(None)
    for raw, canon in _SHOT_TYPE_NORM.items():
        expr = expr.when(e.str.to_lowercase() == raw).then(pl.lit(canon))
    return expr.otherwise(e)


def _event_zone() -> pl.Expr:
    x, xf, eta = pl.col("x"), pl.col("x_fixed"), pl.col("event_team_abbr")
    home, away = pl.col("home_abbr"), pl.col("away_abbr")
    return (
        pl.when((x >= -25) & (x <= 25))
        .then(pl.lit("NZ"))
        .when(((xf < -25) & (eta == home)) | ((xf > 25) & (eta == away)))
        .then(pl.lit("DZ"))
        .when(((xf > 25) & (eta == home)) | ((xf < -25) & (eta == away)))
        .then(pl.lit("OZ"))
        .otherwise(None)
    )


def prepare_xg_data(pbp: pl.DataFrame) -> pl.DataFrame:
    """Port of ``helper_nhl_prepare_xg_data`` — one row per unblocked shot, model features."""
    df = pbp
    for col, default in (("strength_state", "5v5"), ("home_skaters", 5), ("away_skaters", 5)):
        if col not in df.columns:
            df = df.with_columns(pl.lit(default).alias(col))

    df = df.with_columns(secondary_type=_norm_secondary_type())
    df = df.filter(
        (pl.col("period_type") != "SHOOTOUT")
        & ((pl.col("secondary_type") != "Penalty Shot") | pl.col("secondary_type").is_null())
        & (pl.col("event_type") != "CHANGE")
    )
    if df.height == 0:
        return df

    # Lag features within (game_id, period), in time-sorted row order.
    grp = ["game_id", "period"]
    df = (
        df.with_columns(event_zone=_event_zone())
        .with_columns(
            last_event_type=pl.col("event_type").shift(1).over(grp),
            last_event_team=pl.col("event_team_abbr").shift(1).over(grp),
            time_since_last=(pl.col("game_seconds") - pl.col("game_seconds").shift(1).over(grp)),
            last_x=pl.col("x").shift(1).over(grp),
            last_y=pl.col("y").shift(1).over(grp),
            last_event_zone=pl.col("event_zone").shift(1).over(grp),
        )
        .with_columns(
            distance_from_last=(
                ((pl.col("y") - pl.col("last_y")) ** 2 + (pl.col("x") - pl.col("last_x")) ** 2).sqrt().round(1)
            ),
        )
    )

    df = df.filter(pl.col("event_type").is_in(_UNBLOCKED) & pl.col("last_event_type").is_in(_VALID_LAST))
    if df.height == 0:
        return df

    season = pl.col("season").cast(pl.Utf8)
    eta, home = pl.col("event_team_abbr"), pl.col("home_abbr")
    ets = pl.when(eta == home).then(pl.col("home_skaters")).otherwise(pl.col("away_skaters"))
    ots = pl.when(eta == home).then(pl.col("away_skaters")).otherwise(pl.col("home_skaters"))
    last_zone, last_type, tsl = pl.col("last_event_zone"), pl.col("last_event_type"), pl.col("time_since_last")
    df = df.with_columns(
        era_2011_2013=season.is_in(["20102011", "20112012", "20122013"]).cast(pl.Int64),
        era_2014_2018=season.is_in(["20132014", "20142015", "20152016", "20162017", "20172018"]).cast(pl.Int64),
        era_2019_2021=season.is_in(["20182019", "20192020", "20202021"]).cast(pl.Int64),
        era_2022_2024=season.is_in(["20212022", "20222023", "20232024"]).cast(pl.Int64),
        era_2025_on=(season.cast(pl.Float64) > 20232024).cast(pl.Int64),
        total_skaters_on=(ets + ots),
        event_team_advantage=(ets - ots),
        rebound=(last_type.is_in(["SHOT", "MISSED_SHOT", "GOAL"]) & (tsl <= 2)).cast(pl.Int64),
        rush=(last_zone.is_in(["NZ", "DZ"]) & (tsl <= 4)).cast(pl.Int64),
        cross_ice_event=(
            (last_zone == "OZ")
            & (((pl.col("last_y") > 3) & (pl.col("y") < -3)) | ((pl.col("last_y") < -3) & (pl.col("y") > 3)))
            & (tsl <= 2)
        ).cast(pl.Int64),
        empty_net=(pl.col("empty_net").cast(pl.Boolean).fill_null(False)).cast(pl.Int64),
    )

    # One-hot shot_type + last_event_type directly into the model's clean_names columns.
    onehots = {}
    for canon, col in _SHOT_TYPE_COL.items():
        onehots[col] = (pl.col("secondary_type") == canon).cast(pl.Int64)
    for raw, col in _LAST_EVENT_COL.items():
        onehots[col] = (pl.col("last_event_type") == raw).cast(pl.Int64)
    return df.with_columns(**onehots)


def calculate_xg(pbp: pl.DataFrame, models: dict) -> pl.DataFrame:
    """Port of ``helper_nhl_calculate_xg`` — predict per strength state, join xG back by event_id."""
    import xgboost as xgb

    prep = prepare_xg_data(pbp)
    if prep.height == 0:
        return pbp.with_columns(xg=pl.lit(None, dtype=pl.Float64))

    def _matrix(sub: pl.DataFrame, feats: list[str]) -> np.ndarray:
        arr = np.zeros((sub.height, len(feats)), dtype=np.float32)
        present = set(sub.columns)
        for j, f in enumerate(feats):
            if f in present:
                arr[:, j] = sub[f].cast(pl.Float64).fill_null(0).to_numpy()
        return arr

    parts = []
    for sub, feats, booster in (
        (prep.filter(pl.col("strength_state") == "5v5"), models["feats_5v5"], models["m5v5"]),
        (prep.filter(pl.col("strength_state") != "5v5"), models["feats_st"], models["mst"]),
    ):
        if sub.height == 0:
            continue
        dm = xgb.DMatrix(_matrix(sub, feats), feature_names=list(feats))
        preds = booster.predict(dm)
        parts.append(sub.select("event_id").with_columns(xg=pl.Series(preds, dtype=pl.Float64)))

    xg_results = pl.concat(parts) if parts else pl.DataFrame(schema={"event_id": pl.Int64, "xg": pl.Float64})
    out = pbp.join(xg_results, on="event_id", how="left")
    if "secondary_type" in out.columns:
        out = out.with_columns(
            xg=pl.when(pl.col("secondary_type") == "Penalty Shot").then(models["ps"]).otherwise(pl.col("xg")),
        )
    if "event_idx" in out.columns:
        out = out.sort("event_idx")
    return out


# clean_names reference (kept for provenance; the maps above are precomputed from it).
def _clean_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
