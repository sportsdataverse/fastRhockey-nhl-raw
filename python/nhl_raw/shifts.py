"""Faithful Python port of fastRhockey's ``nhl_game_shifts`` (JSON path + aggregate).

Canonical R source: ``fastRhockey/R/nhl_game_shifts.R`` — ``nhl_game_shifts`` +
``.aggregate_shifts``. Fetches the legacy stats-API shiftcharts endpoint and aggregates
per-player-shift records into the per-(team, period, time) CHANGE rows that
``feed.integrate_shifts`` consumes.

The HTML-TOI-report fallback (``.parse_toi_html``, for the growing share of recent games
where the shiftcharts API returns ``{total: 0, data: []}``) is a documented follow-up —
see ``parse_toi_html`` below.
"""

from __future__ import annotations

import polars as pl

from nhl_raw.fetch import get_json

_SHIFTCHARTS = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"


def _to_seconds(col: str) -> pl.Expr:
    parts = pl.col(col).str.split(":")
    return parts.list.get(0, null_on_oob=True).cast(pl.Int64) * 60 + parts.list.get(1, null_on_oob=True).cast(pl.Int64)


def aggregate_shifts(data: list[dict]) -> pl.DataFrame:
    """Port of ``.aggregate_shifts`` (JSON branch) — per-player shifts -> CHANGE rows."""
    raw = pl.DataFrame(data, infer_schema_length=None)
    df = raw.select(
        player_id=pl.col("playerId"),
        player_name=pl.col("firstName") + pl.lit(" ") + pl.col("lastName"),
        team_name=pl.col("teamName"),
        period=pl.col("period").cast(pl.Int64),
        start_time=pl.col("startTime"),
        end_time=pl.col("endTime"),
        duration=pl.col("duration"),
    ).filter(pl.col("duration").is_not_null())
    df = (
        df.with_columns(
            start_seconds=_to_seconds("start_time"),
            end_seconds=_to_seconds("end_time"),
            duration_seconds=_to_seconds("duration"),
        )
        .with_columns(
            start_game_seconds=pl.col("start_seconds") + 1200 * (pl.col("period") - 1),
            end_game_seconds=pl.col("end_seconds") + 1200 * (pl.col("period") - 1),
        )
        .filter(pl.col("duration_seconds") > 0)
    )

    on = (
        df.group_by(["team_name", "period", "start_time", "start_seconds", "start_game_seconds"], maintain_order=True)
        .agg(
            num_on=pl.len(),
            players_on=pl.col("player_name").str.join(", "),
            ids_on=pl.col("player_id").cast(pl.Utf8).str.join(", "),
        )
        .rename({"start_time": "period_time", "start_seconds": "period_seconds", "start_game_seconds": "game_seconds"})
    )
    off = (
        df.group_by(["team_name", "period", "end_time", "end_seconds", "end_game_seconds"], maintain_order=True)
        .agg(
            num_off=pl.len(),
            players_off=pl.col("player_name").str.join(", "),
            ids_off=pl.col("player_id").cast(pl.Utf8).str.join(", "),
        )
        .rename({"end_time": "period_time", "end_seconds": "period_seconds", "end_game_seconds": "game_seconds"})
    )
    out = on.join(
        off, on=["game_seconds", "team_name", "period", "period_time", "period_seconds"], how="full", coalesce=True
    )
    return (
        out.sort("game_seconds")
        .with_columns(
            event=pl.lit("Change"),
            event_type=pl.lit("CHANGE"),
            game_seconds_remaining=3600 - pl.col("game_seconds"),
        )
        .rename({"team_name": "event_team"})
        .with_columns(
            players_on=pl.col("players_on").fill_null("None"),
            players_off=pl.col("players_off").fill_null("None"),
            ids_on=pl.col("ids_on").fill_null("0"),
            ids_off=pl.col("ids_off").fill_null("0"),
        )
    )


def parse_toi_html(game_id: int) -> list[dict] | None:
    """HTML-TOI-report fallback for empty-shiftcharts games (``.parse_toi_html``).

    Not yet ported — a faithful port needs the legacy ``nhl.com/scores/htmlreports``
    parser + the boxscore sweater→player_id crosswalk + ``.smart_titlecase``. Until then
    games whose shiftcharts API returns empty yield no shifts (the enrichment then runs
    without on-ice tracking, as the R path does on a shift fetch failure).
    """
    return None


def nhl_game_shifts(game_id: int, *, session: object | None = None) -> list[dict] | None:
    """Port of ``nhl_game_shifts`` — fetch shiftcharts JSON -> aggregated CHANGE rows.

    Returns a list of per-change records (the shape ``integrate_shifts`` / the raw
    ``shifts`` key expect), or ``None`` when no shift data is available.
    """
    site = get_json(_SHIFTCHARTS.format(game_id=game_id), session=session)
    if site is None:
        return None
    data = site.get("data") or []
    if not data:
        return parse_toi_html(game_id)
    agg = aggregate_shifts(data)
    return agg.to_dicts() if agg.height else None
