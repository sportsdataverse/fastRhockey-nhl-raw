"""Faithful Python port of fastRhockey's NHL play-by-play enrichment.

Canonical R source: ``fastRhockey/R/nhl_game_feed.R`` — ``nhl_game_feed`` →
``.build_pbp`` → ``.parse_plays`` / ``.fix_coordinates`` / ``.add_shot_metrics`` /
``.integrate_shifts`` / ``.add_strength_states`` / ``.add_descriptions`` /
``.finalize_columns`` + ``helper_nhl_calculate_xg``.

Reproduces the ``all_plays`` block of ``final/{game_id}.json`` from the raw
api-web play-by-play payload (+ shifts), so the NHL data pipeline can run without R.

Port status (parity-gated against ``tests/fixtures/nhl_raw/final_2024020001.json``):

* [x] ``parse_plays``     — event / time / coords / players / situation / penalty / secondary / empty-net
* [x] ``fix_coordinates`` — ``homeTeamDefendingSide`` normalization (home shoots right)
* [x] ``add_shot_metrics``— ``shot_distance`` / ``shot_angle``
* [x] ``integrate_shifts`` + ``add_strength_states`` — on-ice cumsum matrix + strength
* [x] ``add_descriptions`` + ``finalize_columns``; xG lives in ``nhl_raw.xg``
"""

from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
import polars as pl

# Human-readable event labels (.parse_plays `event` case_when); unmapped → the type itself.
_EVENT_LABELS: dict[str, str] = {
    "SHOT": "Shot",
    "GOAL": "Goal",
    "MISSED_SHOT": "Missed Shot",
    "BLOCKED_SHOT": "Blocked Shot",
    "HIT": "Hit",
    "FACEOFF": "Faceoff",
    "GIVEAWAY": "Giveaway",
    "TAKEAWAY": "Takeaway",
    "PENALTY": "Penalty",
    "STOP": "Stoppage",
    "PERIOD_START": "Period Start",
    "PERIOD_END": "Period End",
    "GAME_END": "Game End",
    "CHANGE": "Change",
    "DELAYED_PENALTY": "Delayed Penalty",
    "GAME_SCHEDULED": "Game Scheduled",
}

# Coalesce sources for the three event players (.extract_event_players).
_P1_COLS = [
    "details.scoringPlayerId",
    "details.shootingPlayerId",
    "details.hittingPlayerId",
    "details.winningPlayerId",
    "details.committedByPlayerId",
    "details.playerId",
]
_P2_COLS = [
    "details.assist1PlayerId",
    "details.blockingPlayerId",
    "details.goalieInNetId",
    "details.hitteePlayerId",
    "details.losingPlayerId",
    "details.drawnByPlayerId",
]
_P3_COLS = ["details.assist2PlayerId", "details.servedByPlayerId"]

_FENWICK = ["SHOT", "MISSED_SHOT", "GOAL"]
_FENWICK_BLK = ["SHOT", "MISSED_SHOT", "GOAL", "BLOCKED_SHOT"]
_DEG = 180.0 / math.pi


def _ensure(df: pl.DataFrame, cols: list[str], dtype: pl.DataType = pl.Utf8) -> pl.DataFrame:
    """Pad any missing columns with a typed null literal (json_normalize only emits
    columns for keys present in >=1 row, so absent detail keys must be backfilled)."""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        df = df.with_columns([pl.lit(None, dtype=dtype).alias(c) for c in missing])
    return df


def _time_to_seconds(col: str) -> pl.Expr:
    """`MM:SS` → seconds (mirrors .time_to_seconds)."""
    e = pl.col(col)
    parts = e.str.split(":")
    return (
        pl.when(e.is_null() | (e == ""))
        .then(None)
        .otherwise(
            parts.list.get(0, null_on_oob=True).cast(pl.Float64) * 60
            + parts.list.get(1, null_on_oob=True).cast(pl.Float64)
        )
    )


def _season_type(game_id: int) -> str:
    """Season-type code from digits 5-6 of the game id (.build_pbp)."""
    code = int(str(game_id)[4:6])
    return {1: "PR", 2: "R", 3: "P", 4: "A"}.get(code, "R")


def _event_label_expr() -> pl.Expr:
    expr = None
    for k, v in _EVENT_LABELS.items():
        cond = pl.col("event_type") == k
        expr = pl.when(cond).then(pl.lit(v)) if expr is None else expr.when(cond).then(pl.lit(v))
    return expr.otherwise(pl.col("event_type")).alias("event")


def parse_plays(
    plays: list[dict], home_abbr: str, away_abbr: str, home_id: int, away_id: int, game_id: int, season: str
) -> pl.DataFrame:
    """Port of ``.parse_plays`` — the deterministic, shift-independent event parse."""
    if not plays:
        return pl.DataFrame()
    df = pl.from_pandas(pd.json_normalize(plays, sep="."))
    if df.height == 0:
        return pl.DataFrame()

    df = _ensure(
        df,
        [
            "typeDescKey",
            "timeInPeriod",
            "timeRemaining",
            "situationCode",
            # NOTE: homeTeamDefendingSide is intentionally NOT padded here — fix_coordinates
            # keys on its presence (R's `%in% names`) to pick the API path vs the median-x
            # fallback, so padding it would make the fallback dead for games that lack it.
            "details.reason",
            "details.secondaryReason",
            "details.shotType",
            "details.descKey",
            "details.typeCode",
        ],
    )
    df = _ensure(df, ["periodDescriptor.number"], dtype=pl.Int64)
    df = _ensure(df, ["details.xCoord", "details.yCoord"], dtype=pl.Float64)
    df = _ensure(
        df,
        [
            "details.homeScore",
            "details.awayScore",
            "details.eventOwnerTeamId",
            "details.duration",
            *_P1_COLS,
            *_P2_COLS,
            *_P3_COLS,
            "details.goalieInNetId",
        ],
        dtype=pl.Int64,
    )

    # ----- event type + label -----
    df = (
        df.with_columns(
            event_type=pl.col("typeDescKey").str.to_uppercase().str.replace_all("-", "_", literal=True),
        )
        .with_columns(
            event_type=pl.when(pl.col("event_type") == "SHOT_ON_GOAL")
            .then(pl.lit("SHOT"))
            .when(pl.col("event_type") == "STOPPAGE")
            .then(pl.lit("STOP"))
            .otherwise(pl.col("event_type")),
        )
        .with_columns(_event_label_expr())
    )

    # ----- time fields -----
    df = (
        df.with_columns(
            period=pl.col("periodDescriptor.number").cast(pl.Int64),
            period_time=pl.col("timeInPeriod"),
            period_time_remaining=pl.col("timeRemaining"),
        )
        .with_columns(
            period_seconds=_time_to_seconds("period_time"),
            period_seconds_remaining=_time_to_seconds("period_time_remaining"),
        )
        .with_columns(
            game_seconds=pl.col("period_seconds") + 1200 * (pl.col("period") - 1),
            game_seconds_remaining=pl.when(pl.col("period") <= 3)
            .then((3 - pl.col("period")) * 1200 + pl.col("period_seconds_remaining"))
            .otherwise(pl.col("period_seconds_remaining")),
        )
    )

    # ----- coordinates + scores -----
    df = df.with_columns(
        x=pl.col("details.xCoord").cast(pl.Float64, strict=False),
        y=pl.col("details.yCoord").cast(pl.Float64, strict=False),
        home_score=pl.col("details.homeScore").cast(pl.Int64, strict=False),
        away_score=pl.col("details.awayScore").cast(pl.Int64, strict=False),
        event_owner_team_id=pl.col("details.eventOwnerTeamId").cast(pl.Int64, strict=False),
    )

    # ----- event owner team -> abbr / type -----
    df = df.with_columns(
        event_team_abbr=pl.when(pl.col("event_owner_team_id") == home_id)
        .then(pl.lit(home_abbr))
        .when(pl.col("event_owner_team_id") == away_id)
        .then(pl.lit(away_abbr))
        .otherwise(None),
    ).with_columns(
        event_team_type=pl.when(pl.col("event_team_abbr") == home_abbr)
        .then(pl.lit("home"))
        .when(pl.col("event_team_abbr") == away_abbr)
        .then(pl.lit("away"))
        .otherwise(None),
    )

    # ----- blocked shot owns the blocker's team, not the shooter's -----
    is_blk = pl.col("event_type") == "BLOCKED_SHOT"
    df = df.with_columns(
        event_team_abbr=pl.when(is_blk & (pl.col("event_team_abbr") == home_abbr))
        .then(pl.lit(away_abbr))
        .when(is_blk & (pl.col("event_team_abbr") == away_abbr))
        .then(pl.lit(home_abbr))
        .otherwise(pl.col("event_team_abbr")),
        event_owner_team_id=pl.when(is_blk & (pl.col("event_owner_team_id") == home_id))
        .then(pl.lit(away_id))
        .when(is_blk & (pl.col("event_owner_team_id") == away_id))
        .then(pl.lit(home_id))
        .otherwise(pl.col("event_owner_team_id")),
    ).with_columns(
        event_team_type=pl.when(pl.col("event_team_abbr") == home_abbr)
        .then(pl.lit("home"))
        .when(pl.col("event_team_abbr") == away_abbr)
        .then(pl.lit("away"))
        .otherwise(None),
    )

    # ----- event players (coalesce) + types + goalie -----
    df = df.with_columns(
        event_player_1_id=pl.coalesce([pl.col(c).cast(pl.Int64, strict=False) for c in _P1_COLS]),
        event_player_2_id=pl.coalesce([pl.col(c).cast(pl.Int64, strict=False) for c in _P2_COLS]),
        event_player_3_id=pl.coalesce([pl.col(c).cast(pl.Int64, strict=False) for c in _P3_COLS]),
        event_player_4_id=pl.col("details.goalieInNetId").cast(pl.Int64, strict=False),
    )
    et = pl.col("event_type")
    df = df.with_columns(
        event_player_1_type=pl.when(et == "GOAL")
        .then(pl.lit("Scorer"))
        .when(et.is_in(["SHOT", "MISSED_SHOT"]))
        .then(pl.lit("Shooter"))
        .when(et == "HIT")
        .then(pl.lit("Hitter"))
        .when(et == "FACEOFF")
        .then(pl.lit("Winner"))
        .when(et == "PENALTY")
        .then(pl.lit("PenaltyOn"))
        .when(et == "BLOCKED_SHOT")
        .then(pl.lit("Shooter"))
        .when(et.is_in(["GIVEAWAY", "TAKEAWAY"]))
        .then(pl.lit("PlayerID"))
        .otherwise(None),
        event_player_2_type=pl.when(et == "GOAL")
        .then(pl.lit("Assist"))
        .when(et.is_in(["SHOT", "MISSED_SHOT"]))
        .then(pl.lit("Goalie"))
        .when(et == "HIT")
        .then(pl.lit("Hittee"))
        .when(et == "FACEOFF")
        .then(pl.lit("Loser"))
        .when(et == "PENALTY")
        .then(pl.lit("DrewBy"))
        .when(et == "BLOCKED_SHOT")
        .then(pl.lit("Blocker"))
        .otherwise(None),
        event_player_3_type=pl.when((et == "GOAL") & pl.col("event_player_3_id").is_not_null())
        .then(pl.lit("Assist"))
        .when((et == "PENALTY") & pl.col("event_player_3_id").is_not_null())
        .then(pl.lit("ServedBy"))
        .otherwise(None),
        event_player_4_type=pl.when((et == "GOAL") & pl.col("event_player_4_id").is_not_null())
        .then(pl.lit("Goalie"))
        .otherwise(None),
    ).with_columns(
        event_goalie_id=pl.when(pl.col("event_player_2_type") == "Goalie")
        .then(pl.col("event_player_2_id"))
        .when(pl.col("event_player_3_type") == "Goalie")
        .then(pl.col("event_player_3_id"))
        .when(pl.col("event_player_4_type") == "Goalie")
        .then(pl.col("event_player_4_id"))
        .otherwise(None),
    )

    # ----- situation code -> skater / goalie counts -----
    sc = pl.col("situationCode").cast(pl.Utf8)
    df = df.with_columns(
        away_goalie_in=sc.str.slice(0, 1).cast(pl.Int64, strict=False),
        away_skaters=sc.str.slice(1, 1).cast(pl.Int64, strict=False),
        home_skaters=sc.str.slice(2, 1).cast(pl.Int64, strict=False),
        home_goalie_in=sc.str.slice(3, 1).cast(pl.Int64, strict=False),
    )

    # ----- penalty / secondary type / stoppage reason -----
    tc = pl.col("details.typeCode")
    df = df.with_columns(
        typeCode=tc,
        penalty_minutes=pl.col("details.duration").cast(pl.Int64, strict=False),
        penalty_severity=pl.when(tc.is_in(["MIN", "BEN"]))
        .then(pl.lit("Minor"))
        .when(tc == "MAJ")
        .then(pl.lit("Major"))
        .when(tc == "MIS")
        .then(pl.lit("Misconduct"))
        .when(tc == "MAT")
        .then(pl.lit("Match"))
        .when(tc == "GM")
        .then(pl.lit("Game Misconduct"))
        .otherwise(None),
        secondary_type=pl.when(et == "PENALTY").then(pl.col("details.descKey")).otherwise(pl.col("details.shotType")),
        reason=pl.col("details.reason"),
        secondaryReason=pl.col("details.secondaryReason"),
    )

    # ----- empty net / extra attacker -----
    df = df.with_columns(
        empty_net=pl.when(
            et.is_in(["GOAL", "SHOT", "MISSED_SHOT"])
            & (pl.col("event_team_type") == "home")
            & (pl.col("away_goalie_in") == 0)
        )
        .then(True)
        .when(
            et.is_in(["GOAL", "SHOT", "MISSED_SHOT"])
            & (pl.col("event_team_type") == "away")
            & (pl.col("home_goalie_in") == 0)
        )
        .then(True)
        .otherwise(False),
        extra_attacker=pl.when((pl.col("event_team_type") == "home") & (pl.col("home_goalie_in") == 0))
        .then(True)
        .when((pl.col("event_team_type") == "away") & (pl.col("away_goalie_in") == 0))
        .then(True)
        .otherwise(False),
    )

    return df.with_columns(game_id=pl.lit(int(game_id), dtype=pl.Int64))


def fix_coordinates(df: pl.DataFrame, home_abbr: str, away_abbr: str) -> pl.DataFrame:
    """Port of ``.fix_coordinates`` — normalize so the home team always shoots right."""
    x, y, eta = pl.col("x"), pl.col("y"), pl.col("event_team_abbr")
    if "homeTeamDefendingSide" in df.columns:
        hds = pl.col("homeTeamDefendingSide")
        return df.with_columns(
            x_fixed=pl.when(x.is_null())
            .then(None)
            .when((hds == "left") & (eta == home_abbr))
            .then(x.abs())
            .when((hds == "left") & (eta == away_abbr))
            .then(-x.abs())
            .when((hds == "right") & (eta == home_abbr))
            .then(x.abs())
            .when((hds == "right") & (eta == away_abbr))
            .then(-x.abs())
            .otherwise(x),
            y_fixed=pl.when(y.is_null())
            .then(None)
            .when((hds == "left") & (eta == home_abbr) & (x < 0))
            .then(-y)
            .when((hds == "left") & (eta == away_abbr) & (x > 0))
            .then(-y)
            .when((hds == "right") & (eta == home_abbr) & (x > 0))
            .then(-y)
            .when((hds == "right") & (eta == away_abbr) & (x < 0))
            .then(-y)
            .otherwise(y),
        )
    # Fallback: median-x per (team, period, game) when the API omits the defending side.
    med_x = (
        pl.when(pl.col("event_type").is_in(_FENWICK))
        .then(x)
        .otherwise(None)
        .median()
        .over(["event_team_abbr", "period", "game_id"])
    )
    return (
        df.with_columns(med_x=med_x)
        .with_columns(
            x_fixed=pl.when((eta == home_abbr) & (pl.col("med_x") > 0))
            .then(x)
            .when((eta == home_abbr) & (pl.col("med_x") < 0))
            .then(0 - x)
            .when((eta == away_abbr) & (pl.col("med_x") > 0))
            .then(0 - x)
            .when((eta == away_abbr) & (pl.col("med_x") < 0))
            .then(x)
            .otherwise(x),
            y_fixed=pl.when((eta == home_abbr) & (pl.col("med_x") > 0))
            .then(y)
            .when((eta == home_abbr) & (pl.col("med_x") < 0))
            .then(0 - y)
            .when((eta == away_abbr) & (pl.col("med_x") > 0))
            .then(0 - y)
            .when((eta == away_abbr) & (pl.col("med_x") < 0))
            .then(y)
            .otherwise(y),
        )
        .drop("med_x")
    )


def add_shot_metrics(df: pl.DataFrame, home_abbr: str) -> pl.DataFrame:
    """Port of ``.add_shot_metrics`` — distance/angle to the attacked net (x=±89)."""
    xf, yf, eta = pl.col("x_fixed"), pl.col("y_fixed"), pl.col("event_team_abbr")
    in_f = pl.col("event_type").is_in(_FENWICK_BLK)
    is_home = eta == home_abbr
    df = df.with_columns(
        shot_distance=pl.when(in_f & is_home)
        .then((((xf - 89) ** 2 + yf**2).sqrt()).abs().round(1))
        .when(in_f & ~is_home)
        .then((((xf + 89) ** 2 + yf**2).sqrt()).abs().round(1))
        .otherwise(None),
        shot_angle=pl.when(in_f & is_home)
        .then((((0 - yf) / (89 - xf)).arctan().abs() * _DEG).round(1))
        .when(in_f & ~is_home)
        .then((((0 - yf) / (-89 - xf)).arctan().abs() * _DEG).round(1))
        .otherwise(None),
    )
    # Behind-the-net angles wrap past 90°.
    return df.with_columns(
        shot_angle=pl.when(in_f & is_home & (xf > 89))
        .then(180 - pl.col("shot_angle"))
        .when(in_f & ~is_home & (xf < -89))
        .then(180 - pl.col("shot_angle"))
        .otherwise(pl.col("shot_angle")),
    )


def parse_game_rosters(pbp_raw: dict) -> pl.DataFrame:
    """Port of ``.parse_game_rosters`` — rosterSpots -> player lookup (rosterSpots order)."""
    schema = {
        "player_id": pl.Int64,
        "full_name": pl.Utf8,
        "first_name": pl.Utf8,
        "last_name": pl.Utf8,
        "team_abbr": pl.Utf8,
        "team_id": pl.Int64,
        "position_code": pl.Utf8,
        "sweater_number": pl.Int64,
    }
    rs = pbp_raw.get("rosterSpots") or []
    if not rs:
        return pl.DataFrame(schema=schema)
    home, away = pbp_raw.get("homeTeam", {}) or {}, pbp_raw.get("awayTeam", {}) or {}
    home_id, away_id, home_abbr, away_abbr = home.get("id"), away.get("id"), home.get("abbrev"), away.get("abbrev")

    def _default(v: object) -> str | None:
        return v.get("default") if isinstance(v, dict) else v

    recs = []
    for p in rs:
        first, last, tid = _default(p.get("firstName")), _default(p.get("lastName")), p.get("teamId")
        recs.append(
            {
                "player_id": int(p["playerId"]) if p.get("playerId") is not None else None,
                "full_name": f"{first} {last}",
                "first_name": first,
                "last_name": last,
                "team_abbr": home_abbr if tid == home_id else (away_abbr if tid == away_id else None),
                "team_id": int(tid) if tid is not None else None,
                "position_code": p.get("positionCode"),
                "sweater_number": int(p["sweaterNumber"]) if p.get("sweaterNumber") is not None else None,
            }
        )
    return pl.DataFrame(recs, schema=schema)


def _priority_expr() -> pl.Expr:
    """Stable interleave priority (.integrate_shifts) — shots before goals before changes."""
    et = pl.col("event_type")
    return (
        pl.when(et.is_in(["SHOT", "MISSED_SHOT", "BLOCKED_SHOT", "HIT", "GIVEAWAY", "TAKEAWAY"]))
        .then(1)
        .when(et == "GOAL")
        .then(2)
        .when(et == "STOP")
        .then(3)
        .when(et == "PENALTY")
        .then(4)
        .when(et == "CHANGE")
        .then(5)
        .when(et == "PERIOD_END")
        .then(6)
        .when(et == "GAME_END")
        .then(7)
        .when(et == "FACEOFF")
        .then(8)
        .otherwise(9)
        .alias("_priority")
    )


def _uniq(seq: list) -> list:
    seen: set = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def build_onice_matrix(pbp: pl.DataFrame, rosters: pl.DataFrame, home_abbr: str, away_abbr: str) -> pl.DataFrame:
    """Port of ``.build_onice_matrix`` — per-player ``cumsum(on) - cumsum(off)`` over CHANGE rows.

    ``ids_on``/``ids_off`` are ``", "``-joined 7-digit ids; substring containment (R
    ``grepl(fixed=TRUE)``) is safe because a 7-digit id cannot span the separator. Goalies
    (``position_code == "G"``) are pulled out of the skater slots into ``*_goalie_id``.
    """
    home_r = rosters.filter(pl.col("team_abbr") == home_abbr)
    away_r = rosters.filter(pl.col("team_abbr") == away_abbr)
    home_players, away_players = _uniq(home_r["player_id"].to_list()), _uniq(away_r["player_id"].to_list())
    home_goalies = set(home_r.filter(pl.col("position_code") == "G")["player_id"].to_list())
    away_goalies = set(away_r.filter(pl.col("position_code") == "G")["player_id"].to_list())

    n = pbp.height
    ids_on = ["" if v is None else str(v) for v in pbp["ids_on"].to_list()] if "ids_on" in pbp.columns else [""] * n
    ids_off = ["" if v is None else str(v) for v in pbp["ids_off"].to_list()] if "ids_off" in pbp.columns else [""] * n

    def _status(players: list) -> dict:
        out = {}
        for pid in players:
            ps = str(pid)
            on = np.fromiter((ps in s for s in ids_on), dtype=int, count=n)
            off = np.fromiter((ps in s for s in ids_off), dtype=int, count=n)
            out[pid] = np.clip(np.cumsum(on) - np.cumsum(off), 0, 1)
        return out

    def _extract(status: dict, players: list, goalies: set) -> tuple[list, list]:
        skaters = [[None] * 7 for _ in range(n)]
        goalie = [None] * n
        for r in range(n):
            on_ice = [pid for pid in players if status[pid][r] == 1]
            on_g = [pid for pid in on_ice if pid in goalies]
            on_s = [pid for pid in on_ice if pid not in goalies]
            if on_g:
                goalie[r] = on_g[0]
            for k, pid in enumerate(on_s[:7]):
                skaters[r][k] = pid
        return skaters, goalie

    h_sk, h_g = _extract(_status(home_players), home_players, home_goalies)
    a_sk, a_g = _extract(_status(away_players), away_players, away_goalies)

    cols = {}
    for i in range(7):
        cols[f"home_on_{i + 1}_id"] = pl.Series([row[i] for row in h_sk], dtype=pl.Int64)
        cols[f"away_on_{i + 1}_id"] = pl.Series([row[i] for row in a_sk], dtype=pl.Int64)
    cols["home_goalie_id"] = pl.Series(h_g, dtype=pl.Int64)
    cols["away_goalie_id"] = pl.Series(a_g, dtype=pl.Int64)
    pbp = pbp.with_columns(**cols)

    name_map = dict(zip(rosters["player_id"].to_list(), rosters["full_name"].to_list()))
    name_cols = {}
    for side in ("home", "away"):
        for i in range(7):
            name_cols[f"{side}_on_{i + 1}"] = pl.col(f"{side}_on_{i + 1}_id").replace_strict(
                name_map, default=None, return_dtype=pl.Utf8
            )
        name_cols[f"{side}_goalie"] = pl.col(f"{side}_goalie_id").replace_strict(
            name_map, default=None, return_dtype=pl.Utf8
        )
    return pbp.with_columns(**name_cols)


def add_strength_states(pbp: pl.DataFrame, home_abbr: str, away_abbr: str) -> pl.DataFrame:
    """Port of ``.add_strength_states`` — fill skater counts, derive strength state/code/label."""
    non_plays = ["PERIOD_START", "PERIOD_END", "GAME_END", "GAME_SCHEDULED", "CHANGE"]
    pbp = pbp.with_columns(
        home_skaters=pl.col("home_skaters").fill_null(strategy="forward").fill_null(strategy="backward"),
        away_skaters=pl.col("away_skaters").fill_null(strategy="forward").fill_null(strategy="backward"),
    )
    hs, as_, eta, et = pl.col("home_skaters"), pl.col("away_skaters"), pl.col("event_team_abbr"), pl.col("event_type")
    return (
        pbp.with_columns(
            strength_state=pl.when(eta == away_abbr)
            .then(pl.format("{}v{}", as_, hs))
            .otherwise(pl.format("{}v{}", hs, as_)),
            strength_code=pl.when(hs == as_)
            .then(pl.lit("EV"))
            .when(((hs < as_) & (eta == home_abbr)) | ((as_ < hs) & (eta == away_abbr)))
            .then(pl.lit("SH"))
            .when(((hs < as_) & (eta == away_abbr)) | ((as_ < hs) & (eta == home_abbr)))
            .then(pl.lit("PP"))
            .otherwise(None),
        )
        .with_columns(
            strength_code=pl.when(et.is_in(non_plays) | et.shift(-1).is_in(non_plays) | et.shift(1).is_in(non_plays))
            .then(None)
            .otherwise(pl.col("strength_code")),
        )
        .with_columns(
            strength=pl.when(pl.col("strength_code") == "EV")
            .then(pl.lit("Even"))
            .when(pl.col("strength_code") == "SH")
            .then(pl.lit("Shorthanded"))
            .when(pl.col("strength_code") == "PP")
            .then(pl.lit("Power Play"))
            .otherwise(None),
        )
    )


def integrate_shifts(
    pbp: pl.DataFrame, shifts_list: list[dict], rosters: pl.DataFrame, home_abbr: str, away_abbr: str
) -> pl.DataFrame:
    """Port of ``.integrate_shifts`` — interleave shift CHANGE rows, build on-ice + strength."""
    if not shifts_list:
        return pbp
    shifts = pl.DataFrame(shifts_list, infer_schema_length=None)
    needed = [
        "event_type",
        "period",
        "game_seconds",
        "num_on",
        "players_on",
        "ids_on",
        "num_off",
        "players_off",
        "ids_off",
    ]
    if any(c not in shifts.columns for c in needed):
        return pbp
    # Bind pbp (API order) then shifts; stable tiebreak via the bound index (R's bind_rows + arrange).
    bound = (
        pl.concat([pbp, shifts], how="diagonal_relaxed")
        .with_row_index("_oidx")
        .with_columns(_priority_expr())
        .sort(["period", "game_seconds", "_priority", "_oidx"])
    )
    bound = build_onice_matrix(bound, rosters, home_abbr, away_abbr)
    bound = add_strength_states(bound, home_abbr, away_abbr)
    return bound.drop(["_priority", "_oidx"])


def join_event_player_names(pbp: pl.DataFrame, rosters: pl.DataFrame) -> pl.DataFrame:
    """Port of ``.join_event_player_names`` — event player + goalie names from the roster."""
    lookup = dict(zip(rosters["player_id"].to_list(), rosters["full_name"].to_list()))
    cols = {}
    for p in (1, 2, 3):
        idc = f"event_player_{p}_id"
        if idc in pbp.columns:
            cols[f"event_player_{p}_name"] = pl.col(idc).replace_strict(lookup, default=None, return_dtype=pl.Utf8)
    if "event_goalie_id" in pbp.columns:
        cols["event_goalie_name"] = pl.col("event_goalie_id").replace_strict(lookup, default=None, return_dtype=pl.Utf8)
    return pbp.with_columns(**cols)


def add_descriptions(pbp: pl.DataFrame, home_abbr: str, away_abbr: str) -> pl.DataFrame:
    """Port of ``.add_descriptions`` — event_idx/event_id + the glue-string narratives."""
    for col in ("players_on", "players_off", "reason"):
        if col not in pbp.columns:
            pbp = pbp.with_columns(pl.lit(None, dtype=pl.Utf8).alias(col))
    pbp = pbp.with_columns(event_idx=pl.int_range(pl.len()).cast(pl.Int64)).with_columns(event_id=pl.col("event_idx"))
    et = pl.col("event_type")
    p1, p2, p3 = pl.col("event_player_1_name"), pl.col("event_player_2_name"), pl.col("event_player_3_name")
    g, st, per = pl.col("event_goalie_name"), pl.col("secondary_type"), pl.col("period")
    description = (
        pl.when(et == "PERIOD_START")
        .then(pl.format("Start of Period {}", per))
        .when(et == "PERIOD_END")
        .then(pl.format("End of Period {}", per))
        .when(et == "GAME_END")
        .then(pl.lit("Game End"))
        .when(et == "FACEOFF")
        .then(pl.format("{} faceoff won against {}", p1, p2))
        .when(et == "BLOCKED_SHOT")
        .then(pl.format("{} shot blocked by {}", p1, p2))
        .when(et == "CHANGE")
        .then(pl.format("ON: {}; OFF: {}", pl.col("players_on"), pl.col("players_off")))
        .when(et == "GIVEAWAY")
        .then(pl.format("Giveaway by {}", p1))
        .when(et == "TAKEAWAY")
        .then(pl.format("Takeaway by {}", p1))
        .when(et == "HIT")
        .then(pl.format("{} hit {}", p1, p2))
        .when(et == "MISSED_SHOT")
        .then(pl.format("{} shot missed wide of net", p1))
        .when(et == "PENALTY")
        .then(pl.format("{} {}", p1, st))
        .when(et == "SHOT")
        .then(pl.format("{} shot on goal saved by {}", p1, g))
        .when((et == "STOP") & pl.col("reason").is_not_null())
        .then(pl.format("Stoppage in play ({})", pl.col("reason")))
        .when(et == "STOP")
        .then(pl.lit("Stoppage in play"))
        .when((et == "GOAL") & pl.col("event_player_3_id").is_not_null())
        .then(pl.format("{} {}, assists: {}, {}", p1, st, p2, p3))
        .when((et == "GOAL") & pl.col("event_player_2_id").is_not_null() & (pl.col("event_player_2_type") == "Assist"))
        .then(pl.format("{} {}, assists: {}", p1, st, p2))
        .when(et == "GOAL")
        .then(pl.format("{} {}, unassisted", p1, st))
        .otherwise(None)
    )
    return pbp.with_columns(description=description)


# Preferred column order (.finalize_columns); processed raw-API columns are dropped.
_PREFERRED = [
    "event_type",
    "event",
    "secondary_type",
    "event_team_abbr",
    "event_team_type",
    "description",
    "period",
    "period_type",
    "period_time",
    "period_seconds",
    "period_seconds_remaining",
    "period_time_remaining",
    "game_seconds",
    "game_seconds_remaining",
    "home_score",
    "away_score",
    "event_player_1_name",
    "event_player_1_type",
    "event_player_1_id",
    "event_player_2_name",
    "event_player_2_type",
    "event_player_2_id",
    "event_player_3_name",
    "event_player_3_type",
    "event_player_3_id",
    "event_goalie_name",
    "event_goalie_id",
    "penalty_severity",
    "penalty_minutes",
    "strength_state",
    "strength_code",
    "strength",
    "empty_net",
    "extra_attacker",
    "x",
    "y",
    "x_fixed",
    "y_fixed",
    "shot_distance",
    "shot_angle",
    "home_skaters",
    "away_skaters",
    "home_on_1",
    "home_on_2",
    "home_on_3",
    "home_on_4",
    "home_on_5",
    "home_on_6",
    "home_on_7",
    "away_on_1",
    "away_on_2",
    "away_on_3",
    "away_on_4",
    "away_on_5",
    "away_on_6",
    "away_on_7",
    "home_goalie",
    "away_goalie",
    "num_on",
    "players_on",
    "num_off",
    "players_off",
    "game_id",
    "season",
    "season_type",
    "home_abbr",
    "away_abbr",
    "event_idx",
    "event_id",
]
_DROP_RE = re.compile(
    r"^(details\.|periodDescriptor\.|typeDescKey|typeCode$|situationCode$|sortOrder$|eventId$"
    r"|homeTeamDefendingSide$|timeInPeriod$|timeRemaining$|event_owner_team_id$|event_player_4)"
)


def finalize_columns(pbp: pl.DataFrame) -> pl.DataFrame:
    """Port of ``.finalize_columns`` — preferred order first, then drop processed raw columns."""
    available = [c for c in _PREFERRED if c in pbp.columns]
    remaining = [c for c in pbp.columns if c not in _PREFERRED and not _DROP_RE.match(c)]
    return pbp.select(available + remaining)


def build_pbp(
    pbp_raw: dict,
    game_id: int,
    shifts: list[dict] | None = None,
    include_shifts: bool = True,
    xg: object | None = None,
) -> pl.DataFrame:
    """Build the enriched PBP frame from the raw api-web play-by-play payload (+ shifts).

    parse → fix coordinates → shot metrics → shift integration (on-ice + strength) →
    score LOCF → period type → event-player names → descriptions → finalize → xG
    (when ``xg`` models are supplied via ``nhl_raw.xg.load_xg_models``).
    """
    home, away = pbp_raw.get("homeTeam", {}) or {}, pbp_raw.get("awayTeam", {}) or {}
    home_abbr, away_abbr = home.get("abbrev"), away.get("abbrev")
    df = parse_plays(
        pbp_raw.get("plays") or [],
        home_abbr,
        away_abbr,
        home.get("id"),
        away.get("id"),
        int(game_id),
        str(pbp_raw.get("season")),
    )
    if df.height == 0:
        return df
    df = fix_coordinates(df, home_abbr, away_abbr)
    df = add_shot_metrics(df, home_abbr)

    rosters = parse_game_rosters(pbp_raw)
    if include_shifts and shifts:
        df = integrate_shifts(df, shifts, rosters, home_abbr, away_abbr)
        # Score LOCF over the time-sorted frame (leading nulls -> 0).
        df = df.with_columns(
            home_score=pl.col("home_score").fill_null(strategy="forward").fill_null(0),
            away_score=pl.col("away_score").fill_null(strategy="forward").fill_null(0),
        )

    st = _season_type(int(game_id))
    is_regular, is_playoff, period = st in ("R", "PR"), st == "P", pl.col("period")
    df = df.with_columns(
        period_type=pl.when(period < 4)
        .then(pl.lit("REGULAR"))
        .when(pl.lit(is_regular) & (period == 4))
        .then(pl.lit("OVERTIME"))
        .when(pl.lit(is_regular) & (period == 5))
        .then(pl.lit("SHOOTOUT"))
        .when(pl.lit(is_playoff) & (period > 3))
        .then(pl.lit("OVERTIME"))
        .otherwise(pl.lit("REGULAR")),
    )
    df = join_event_player_names(df, rosters)
    df = add_descriptions(df, home_abbr, away_abbr)
    df = df.with_columns(
        season=pl.lit(str(pbp_raw.get("season"))),
        season_type=pl.lit(st),
        home_abbr=pl.lit(home_abbr),
        away_abbr=pl.lit(away_abbr),
    )
    df = finalize_columns(df)

    if xg is not None:
        from nhl_raw.xg import calculate_xg

        df = calculate_xg(df, xg)
    return df
