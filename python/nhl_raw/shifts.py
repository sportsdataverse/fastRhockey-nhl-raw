"""Faithful Python port of fastRhockey's ``nhl_game_shifts`` (JSON + HTML-TOI fallback).

Canonical R source: ``fastRhockey/R/nhl_game_shifts.R`` — ``nhl_game_shifts`` /
``.aggregate_shifts`` / ``.parse_toi_html`` / ``.smart_titlecase``. Both paths produce the
same per-player ``shifts_raw`` shape, then ``_aggregate`` collapses it to the per-(team,
period, time) CHANGE rows ``feed.integrate_shifts`` consumes.

Source of truth is the legacy stats-API shiftcharts endpoint; when it returns
``{total: 0, data: []}`` (common for recent games) we fall back to scraping the legacy
HTML TOI reports, mapping sweater numbers to player_ids via the boxscore.
"""

from __future__ import annotations

import re

import polars as pl
import requests

from nhl_raw.boxscore import parse_boxscore
from nhl_raw.fetch import _UA, fetch_endpoint, get_json

_SHIFTCHARTS = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"
_TOI = "https://www.nhl.com/scores/htmlreports/{season}/T{side}{gameno}.HTM"

# Per-player shifts_raw columns shared by the JSON + HTML paths -> _aggregate.
_RAW_COLS = [
    "team_name",
    "player_id",
    "player_name",
    "period",
    "start_time",
    "start_seconds",
    "start_game_seconds",
    "end_time",
    "end_seconds",
    "end_game_seconds",
    "duration_seconds",
]


def _to_seconds(col: str) -> pl.Expr:
    parts = pl.col(col).str.split(":")
    return parts.list.get(0, null_on_oob=True).cast(pl.Int64) * 60 + parts.list.get(1, null_on_oob=True).cast(pl.Int64)


def _with_seconds(df: pl.DataFrame) -> pl.DataFrame:
    """Add the start/end/duration second columns + the duration>0 filter (shared)."""
    return (
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


def _normalize_json(data: list[dict]) -> pl.DataFrame:
    """JSON shiftcharts records -> per-player shifts_raw (``.aggregate_shifts`` JSON branch)."""
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
    return _with_seconds(df).select(_RAW_COLS)


def _aggregate(raw: pl.DataFrame) -> pl.DataFrame:
    """Port of ``.aggregate_shifts`` — per-player shifts -> CHANGE rows (shared by both paths)."""
    on = (
        raw.group_by(["team_name", "period", "start_time", "start_seconds", "start_game_seconds"], maintain_order=True)
        .agg(
            num_on=pl.len(),
            players_on=pl.col("player_name").str.join(", "),
            ids_on=pl.col("player_id").cast(pl.Utf8).str.join(", "),
        )
        .rename({"start_time": "period_time", "start_seconds": "period_seconds", "start_game_seconds": "game_seconds"})
    )
    off = (
        raw.group_by(["team_name", "period", "end_time", "end_seconds", "end_game_seconds"], maintain_order=True)
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


def _smart_titlecase(x: str | None) -> str | None:
    """Port of ``.smart_titlecase`` — title-case + fix Mc/Mac/apostrophe prefixes."""
    if not x:
        return x
    s = x.title()

    def fix(s: str, pattern: str) -> str:
        return re.sub(pattern, lambda m: m.group(0)[:-1] + m.group(0)[-1].upper(), s)

    s = fix(s, r"\bMac[a-z]")  # Mac before Mc so "Macdonald" -> "MacDonald"
    s = fix(s, r"\bMc[a-z]")
    s = fix(s, r"'[a-z]")
    return s


def _lf_to_name(lf: str | None) -> str | None:
    """`LAST, FIRST` (uppercase TOI heading) -> proper-case `First Last`."""
    if not lf:
        return lf
    parts = re.split(r",\s*", lf, maxsplit=1)
    if len(parts) == 2:
        return f"{_smart_titlecase(parts[1].strip())} {_smart_titlecase(parts[0].strip())}"
    return _smart_titlecase(lf)


def _parse_toi_side(season: str, gameno: str, side: str, session: requests.Session | None) -> list[dict]:
    """Walk one TOI report (TH=home / TV=visitor): player headings + shift rows in doc order."""
    from bs4 import BeautifulSoup

    url = _TOI.format(season=season, side=side, gameno=gameno)
    try:
        r = (session or requests).get(url, timeout=45, headers=_UA)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    head = soup.find("td", class_="teamHeading")
    if head is None or not head.get_text(strip=True):
        return []
    team_name = _smart_titlecase(head.get_text(strip=True))

    def is_node(t: object) -> bool:
        cls = getattr(t, "get", lambda *_: None)("class") or []
        return (t.name == "td" and "playerHeading" in cls) or (
            t.name == "tr" and ("oddColor" in cls or "evenColor" in cls)
        )

    rows, sweater, lf = [], None, None
    for node in soup.find_all(is_node):
        if node.name == "td":
            m = re.match(r"^(\d+)\s+(.*)$", node.get_text(strip=True))
            if m:
                sweater, lf = int(m.group(1)), m.group(2)
            continue
        cells = [td.get_text(strip=True) for td in node.find_all("td")]
        if len(cells) != 6 or not re.match(r"^\d+$", cells[0]) or not re.match(r"^\d+$", cells[1]):
            continue
        if ":" not in cells[2] or sweater is None:
            continue
        rows.append(
            {
                "team_name": team_name,
                "side": side,
                "sweater_number": sweater,
                "last_first": lf,
                "period": int(cells[1]),
                "start_time": re.sub(r"^(\d+:\d+).*", r"\1", cells[2]),
                "end_time": re.sub(r"^(\d+:\d+).*", r"\1", cells[3]),
                "duration": cells[4],
            }
        )
    return rows


def parse_toi_html(game_id: int, session: requests.Session | None = None) -> pl.DataFrame | None:
    """Port of ``.parse_toi_html`` — legacy HTML TOI reports -> per-player shifts_raw.

    Maps sweater numbers to player_ids via the boxscore (join on home_away + sweater).
    """
    gid = str(game_id)
    if not re.match(r"^[0-9]{10}$", gid):
        return None
    season = f"{gid[:4]}{int(gid[:4]) + 1}"
    gameno = gid[4:10]

    rows = _parse_toi_side(season, gameno, "H", session) + _parse_toi_side(season, gameno, "V", session)
    if not rows:
        return None
    box_raw = fetch_endpoint(game_id, "boxscore", session=session)
    if box_raw is None:
        return None
    box = parse_boxscore(box_raw)
    cols = ["home_away", "sweater_number", "player_id", "team_id", "team_abbrev"]
    lookup = pl.concat([box["skater_stats"].select(cols), box["goalie_stats"].select(cols)], how="vertical")

    name_map = {lf: _lf_to_name(lf) for lf in {r["last_first"] for r in rows}}
    df = pl.DataFrame(rows).with_columns(
        home_away=pl.when(pl.col("side") == "H").then(pl.lit("home")).otherwise(pl.lit("away")),
        player_name=pl.col("last_first").replace_strict(name_map, default=None, return_dtype=pl.Utf8),
    )
    df = df.join(lookup, on=["home_away", "sweater_number"], how="left").filter(pl.col("player_id").is_not_null())
    if df.height == 0:
        return None
    return _with_seconds(df).select(_RAW_COLS)


def nhl_game_shifts(game_id: int, *, session: requests.Session | None = None) -> list[dict] | None:
    """Port of ``nhl_game_shifts`` — shiftcharts JSON (or HTML fallback) -> CHANGE rows."""
    site = get_json(_SHIFTCHARTS.format(game_id=game_id), session=session)
    # Both a failed shiftcharts fetch (site is None) and a populated-but-empty {data: []}
    # fall back to the HTML TOI reports, which may still carry the shifts.
    data = (site or {}).get("data") or []
    raw = _normalize_json(data) if data else parse_toi_html(game_id, session=session)
    if raw is None or raw.height == 0:
        return None
    return _aggregate(raw).to_dicts()
