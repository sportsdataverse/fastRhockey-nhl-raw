"""Contract tests for the per-player bio capture (stage 02).

Offline: the transport is injected, so nothing here touches api-web.nhle.com.
What is pinned is the capture contract that makes the stage safe to re-run and
safe to schedule -- presence-is-not-validity, atomic writes, and the refusal to
bank a payload that lacks the field the stage exists for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from nhl_raw.players import (  # noqa: E402
    MIN_BYTES,
    already_captured,
    player_path,
    scrape_players,
)


def _payload(pid: int = 8478402, shoots: str | None = "L") -> dict:
    # padded so the fixture clears MIN_BYTES the way a real landing payload does
    return {
        "playerId": pid,
        "shootsCatches": shoots,
        "position": "C",
        "heightInInches": 73,
        "weightInPounds": 194,
        "_pad": "x" * MIN_BYTES,
    }


def test_captured_requires_the_field_the_stage_exists_for(tmp_path):
    """A 200 that omits shootsCatches is NOT a capture.

    Presence is not validity: if a payload without handedness counted as done,
    the presence-based resume would never retry it and the gap would be
    permanent and invisible.
    """
    p = player_path(tmp_path, 8478402)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_payload(shoots=None)), encoding="utf-8")
    assert already_captured(p) is False

    p.write_text(json.dumps(_payload(shoots="L")), encoding="utf-8")
    assert already_captured(p) is True


def test_truncated_or_tiny_file_is_not_captured(tmp_path):
    p = player_path(tmp_path, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"playerId": 1, "shootsCatches": "L"}', encoding="utf-8")  # valid JSON, too small
    assert already_captured(p) is False
    p.write_text("{not json", encoding="utf-8")
    assert already_captured(p) is False


def test_scrape_is_idempotent_and_writes_nothing_for_missing_players(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake(pid, session=None):
        calls.append(str(pid))
        return _payload(int(pid)) if str(pid) != "999" else None

    monkeypatch.setattr("nhl_raw.players.fetch_player", fake)

    r1 = scrape_players(["8478402", "999"], tmp_path, sleep_s=0, log=lambda *_: None)
    assert (r1["captured"], r1["missing"], r1["failed"]) == (1, 1, 0)
    # the player with no landing record leaves NO file -- an empty payload on
    # disk would read as captured on the next run
    assert not player_path(tmp_path, 999).exists()

    calls.clear()
    r2 = scrape_players(["8478402", "999"], tmp_path, sleep_s=0, log=lambda *_: None)
    assert r2["captured"] == 0  # already-captured is skipped
    assert calls == ["999"]  # only the un-captured one is retried


def test_limit_zero_means_zero(tmp_path, monkeypatch):
    monkeypatch.setattr("nhl_raw.players.fetch_player", lambda pid, session=None: _payload(int(pid)))
    r = scrape_players(["8478402"], tmp_path, limit=0, sleep_s=0, log=lambda *_: None)
    assert r["captured"] == 0


def test_negative_limit_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        scrape_players(["1"], tmp_path, limit=-1, sleep_s=0, log=lambda *_: None)


def test_a_fetch_failure_is_counted_not_swallowed(tmp_path, monkeypatch):
    """One bad player must not abort a 3,000-player sweep, but it must be
    VISIBLE -- the stage exits non-zero on failures so a cron can tell a
    transport problem from a player who simply has no landing record."""

    def boom(pid, session=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("nhl_raw.players.fetch_player", boom)
    r = scrape_players(["1", "2"], tmp_path, sleep_s=0, log=lambda *_: None)
    assert r["failed"] == 2 and r["captured"] == 0
