#!/usr/bin/env python
"""Capture NHL pregame state (probable goalies + the league injury list), one JSON per game date.

Probable starters and injury lists are current-state feeds: once a game is played the
pregame answer is gone, so history exists only if it is captured before puck drop. Each
run is ONE probe: it fetches the ESPN Site v2 scoreboard for the ET game date and the
league injury list (two requests), and keeps the capture only if it differs from the
previous kept one. The fetch + parse live in sdv-py
(``sportsdataverse.espn_snapshots.parse_probables_snapshot`` /
``parse_injuries_snapshot``); this stage only decides what to keep.

Layout: ``nhl/pregame/{END_YEAR}/{YYYY-MM-DD}.json``::

    {"date": "2026-11-25",
     "probed": ["2026-11-25T11:00:04Z", ...],          # every probe, kept or not
     "events": [{"event_id", "event_date", "home_id", "home_abbreviation",
                 "away_id", "away_abbreviation"}, ...],  # ESPN events of the date
     "captures": [{"captured_at": "...Z", "probables": [...], "injuries": [...]}, ...]}

``probed`` is what a coverage check reads (no probe gap over 90 minutes inside the
window); ``captures`` holds only state changes. A date with no NHL games writes nothing.
The droplet cron runs this every 30 minutes from 11:00 to 03:00 UTC, October to June
(``scripts/nhl_pregame_snapshot.sh``, which also commits the day's file at the 03:00 run).

    python python/nhl_raw_03_pregame_snapshot.py               # today's ET game date
    python python/nhl_raw_03_pregame_snapshot.py 2026-11-27    # another date (probables appear days ahead)

Env: ``NHL_PREGAME_DIR`` overrides the output root (default ``nhl/pregame`` under the repo).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parents[1]

#: Probable columns kept. ``event_state`` (pre/in/post) is left out of the comparison so a
#: puck drop alone is not a state change; the compiler reads puck drop from ``event_date``.
_PROBABLE_COLS = (
    "event_id",
    "team_id",
    "team_abbreviation",
    "athlete_id",
    "athlete_display_name",
    "role",
    "status",
)
#: Injury columns kept. ESPN's per-record news date and comments are left out: they change
#: with every news blurb while the player's status does not.
_INJURY_COLS = (
    "team_id",
    "athlete_id",
    "athlete_display_name",
    "athlete_position",
    "status",
    "detail_type",
    "detail_return_date",
)


def end_year(day: dt.date) -> int:
    """END-year season key (2027 = 2026-27), rolling forward in September."""
    return day.year + 1 if day.month >= 9 else day.year


def _rows(df, cols) -> list[dict]:
    return [{k: r[k] for k in cols} for r in df.iter_rows(named=True)]


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


class DegradedPayload(RuntimeError):
    """A 200 answer that cannot be an observation: no probe is recorded, so the gap shows."""


def capture(day: dt.date, root: Path, *, scoreboard: dict, injuries: dict, now: dt.datetime) -> dict:
    """Fold one probe (two raw payloads) into ``day``'s file.

    Args:
        day: ET game date; the file is ``root/{END_YEAR}/{day}.json``.
        root: Output root (``nhl/pregame``).
        scoreboard: Raw ``espn_nhl_scoreboard(dates=day, return_parsed=False)`` payload.
        injuries: Raw ``espn_nhl_injuries(return_parsed=False)`` payload.
        now: When both payloads had arrived (aware); becomes ``captured_at``.

    Returns:
        ``{"events": <games on the date>, "kept": 1 if the capture was a state change else 0}``.

    Raises:
        DegradedPayload: The injury payload has no ``injuries`` list, or the probables or the
            injury list came back empty where the previous kept capture had rows. Recording
            either would close every open interval and reopen it on the next probe.
    """
    from sportsdataverse._common_espn_parsers import parse_scoreboard
    from sportsdataverse.espn_snapshots import (
        parse_injuries_snapshot,
        parse_probables_snapshot,
    )

    events = parse_scoreboard(scoreboard)
    if events.height == 0:
        return {"events": 0, "kept": 0}
    path = root / str(end_year(day)) / f"{day.isoformat()}.json"
    doc = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"date": day.isoformat(), "probed": [], "events": [], "captures": []}
    )
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    known = {e["event_id"]: e for e in doc["events"]}
    for e in events.iter_rows(named=True):
        known[e["game_id"]] = {
            "event_id": e["game_id"],
            "event_date": e["date"],
            "home_id": e["home_id"],
            "home_abbreviation": e["home_abbreviation"],
            "away_id": e["away_id"],
            "away_abbreviation": e["away_abbreviation"],
        }
    doc["events"] = sorted(known.values(), key=lambda e: (e["event_date"] or "", e["event_id"]))
    probables = sorted(
        _rows(parse_probables_snapshot(scoreboard, league="nhl"), _PROBABLE_COLS),
        key=lambda r: (r["event_id"] or 0, r["team_id"] or 0, r["role"] or ""),
    )
    hurt = sorted(
        _rows(
            parse_injuries_snapshot(injuries, league="nhl", as_of_date=day),
            _INJURY_COLS,
        ),
        key=lambda r: (r["team_id"] or 0, r["athlete_id"] or 0),
    )
    last = doc["captures"][-1] if doc["captures"] else None
    if not isinstance(injuries.get("injuries"), list):
        raise DegradedPayload("injuries payload has no 'injuries' list")
    for name, rows in (("probables", probables), ("injuries", hurt)):
        if last and last[name] and not rows:
            raise DegradedPayload(f"{name} came back empty after a capture with {len(last[name])} rows")
    doc["probed"].append(stamp)
    kept = last is None or (last["probables"], last["injuries"]) != (probables, hurt)
    if kept:
        doc["captures"].append({"captured_at": stamp, "probables": probables, "injuries": hurt})
    _write_atomic(path, doc)
    return {"events": events.height, "kept": int(kept)}


def main(argv: list[str]) -> int:
    from sportsdataverse.nhl import espn_nhl_injuries, espn_nhl_scoreboard

    day = dt.date.fromisoformat(argv[1]) if len(argv) > 1 else dt.datetime.now(ZoneInfo("America/New_York")).date()
    root = Path(os.environ.get("NHL_PREGAME_DIR") or _REPO / "nhl" / "pregame")
    # Both requests before any write: a failed fetch records no probe, so the coverage check
    # sees the gap instead of a probe that observed nothing.
    scoreboard = espn_nhl_scoreboard(dates=day.strftime("%Y%m%d"), return_parsed=False)
    injuries = espn_nhl_injuries(return_parsed=False)
    # Stamped AFTER both responses arrived: valid_from must never precede what was observed,
    # or a state published during the fetch would read as known a few seconds early.
    now = dt.datetime.now(dt.timezone.utc)
    c = capture(day, root, scoreboard=scoreboard, injuries=injuries, now=now)
    print(
        f"[{now:%Y-%m-%dT%H:%M:%SZ}] {day} events={c['events']} kept={c['kept']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
