"""Stage 03 (pregame snapshot) on two real probes of the 2026-11-25 slate.

Fixtures (``tests/fixtures/pregame/``) are ESPN Site v2 captures fetched 2026-11-26 at
00:10:47Z and 00:25:49Z, trimmed to the fields the stage reads (the parsed rows are
identical to the full payloads'). Between the two, one injury moved: ESPN team 5,
athlete 4697436, Day-To-Day -> Out. The goalies did not change.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import nhl_raw_03_pregame_snapshot as stage
import pytest

FIX = Path(__file__).parent / "fixtures" / "pregame"
DAY = dt.date(2026, 11, 25)
T1, T2 = "2026-11-26T001047Z", "2026-11-26T002549Z"


def _probe(root: Path, ts: str, *, injuries_ts: str | None = None) -> dict:
    load = lambda kind, t: json.loads((FIX / f"{kind}_nhl_{t}.json").read_text(encoding="utf-8"))  # noqa: E731
    now = dt.datetime.strptime(ts, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    return stage.capture(
        DAY,
        root,
        scoreboard=load("scoreboard", ts),
        injuries=load("injuries", injuries_ts or ts),
        now=now,
    )


def _doc(root: Path) -> dict:
    return json.loads((root / "2027" / "2026-11-25.json").read_text(encoding="utf-8"))


def test_first_probe_keeps_both_goalies_of_every_game(tmp_path):
    assert _probe(tmp_path, T1) == {"events": 14, "kept": 1}
    doc = _doc(tmp_path)
    assert doc["date"] == "2026-11-25" and doc["probed"] == ["2026-11-26T00:10:47Z"]
    assert len(doc["events"]) == 14
    (cap,) = doc["captures"]
    assert len(cap["probables"]) == 28 and len(cap["injuries"]) == 116
    assert {p["role"] for p in cap["probables"]} == {"probableStartingGoalie"}
    assert all(p["athlete_id"] for p in cap["probables"])


def test_an_unchanged_probe_is_recorded_but_not_kept(tmp_path):
    _probe(tmp_path, T1)
    # the later scoreboard (same goalies; more games under way) with the first injury list:
    # logged in `probed`, no second capture -- a puck drop alone is not a state change
    assert _probe(tmp_path, T2, injuries_ts=T1) == {"events": 14, "kept": 0}
    doc = _doc(tmp_path)
    assert len(doc["probed"]) == 2 and len(doc["captures"]) == 1


def test_an_injury_status_change_is_a_new_capture(tmp_path):
    _probe(tmp_path, T1)
    assert _probe(tmp_path, T2)["kept"] == 1
    doc = _doc(tmp_path)
    assert doc["probed"] == ["2026-11-26T00:10:47Z", "2026-11-26T00:25:49Z"]
    before, after = ({(i["team_id"], i["athlete_id"]): i["status"] for i in c["injuries"]} for c in doc["captures"])
    changed = {k: (before.get(k), after.get(k)) for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert changed == {(5, 4697436): ("Day-To-Day", "Out")}
    assert doc["captures"][0]["probables"] == doc["captures"][1]["probables"]


def test_a_date_without_games_writes_nothing(tmp_path):
    out = stage.capture(
        DAY,
        tmp_path,
        scoreboard={"events": []},
        injuries={},
        now=dt.datetime.now(dt.timezone.utc),
    )
    assert out == {"events": 0, "kept": 0} and not any(tmp_path.rglob("*.json"))


def test_end_year_rolls_in_september():
    assert [stage.end_year(dt.date(y, m, 1)) for y, m in ((2026, 8), (2026, 9), (2027, 6))] == [2026, 2027, 2027]


def test_a_degraded_answer_records_no_probe(tmp_path):
    """A 200 with an empty injury list (or none at all) after a real capture is refused, not kept."""
    _probe(tmp_path, T1)
    sb = json.loads((FIX / f"scoreboard_nhl_{T2}.json").read_text(encoding="utf-8"))
    now = dt.datetime(2026, 11, 26, 0, 40, tzinfo=dt.timezone.utc)
    for injuries in ({"injuries": []}, {}):
        with pytest.raises(stage.DegradedPayload):
            stage.capture(DAY, tmp_path, scoreboard=sb, injuries=injuries, now=now)
    doc = _doc(tmp_path)
    assert len(doc["probed"]) == 1 and len(doc["captures"]) == 1
