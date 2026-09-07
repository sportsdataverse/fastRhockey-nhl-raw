"""Contract tests for the per-player bio capture (stage 02).

Offline: the transport is injected, so nothing here touches api-web.nhle.com.
What is pinned is the capture contract that makes the stage safe to re-run and
safe to schedule -- presence-is-not-validity, atomic writes, and the refusal to
bank a payload that lacks the field the stage exists for.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from nhl_raw.players import (  # noqa: E402
    already_captured,
    player_path,
    scrape_players,
    write_bio_index,
)


def _payload(pid: int = 8478402, shoots: str | None = "L") -> dict:
    # padded so the fixture clears MIN_BYTES the way a real landing payload does
    return {
        "playerId": pid,
        "shootsCatches": shoots,
        "position": "C",
        "heightInInches": 73,
        "weightInPounds": 194,
        "_pad": "x" * 900,
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


def test_validity_is_fields_not_size(tmp_path):
    """A payload that parses and carries the required fields IS captured, however
    small. This test used to assert the opposite, encoding a size floor that put
    five real players in a permanent refetch loop -- their complete payloads are
    as small as 571 bytes because they have almost no career rows."""
    p = player_path(tmp_path, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"playerId": 1, "shootsCatches": "L"}', encoding="utf-8")
    assert already_captured(p) is True

    p.write_text("{not json", encoding="utf-8")
    assert already_captured(p) is False
    p.write_text('{"playerId": 1}', encoding="utf-8")  # parses, but no handedness
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


class _FakeResp:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise requests.exceptions.JSONDecodeError("no json", "", 0)
        return self._body


class _FakeSession:
    """Stands in for requests.Session at the TRANSPORT boundary, so these tests
    exercise the real get_json path rather than a stubbed fetch_player."""

    def __init__(self, *responses, raises=None):
        self.responses, self.raises, self.calls = list(responses), raises, 0

    def get(self, url, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def test_a_404_is_no_record_and_the_run_still_succeeds(tmp_path):
    """A retired player with no landing record is NOT a failure -- old rosters
    are full of them and one must not redden a scheduled sweep."""
    sess = _FakeSession(_FakeResp(404))
    r = scrape_players(["1"], tmp_path, sleep_s=0, session=sess, log=lambda *_: None)
    assert (r["captured"], r["missing"], r["failed"]) == (0, 1, 0)


@pytest.mark.parametrize(
    "kw",
    [
        {"responses": [_FakeResp(503)]},                      # transient, budget exhausted
        {"responses": [_FakeResp(403)]},                      # permanent, but NOT a 404
        {"responses": [_FakeResp(200, bad_json=True)]},       # 200 with an unparseable body
        {"raises": requests.exceptions.ConnectionError("reset")},
        {"raises": requests.exceptions.Timeout("stalled")},
    ],
    ids=["503-exhausted", "403", "bad-json-body", "connection-reset", "timeout"],
)
def test_every_http_failure_is_counted_as_failed_not_as_no_record(tmp_path, monkeypatch, kw):
    """THE regression this suite exists for.

    The transport's default collapses 403, an exhausted 429/5xx budget, a
    connection reset, a timeout and an unparseable body all to ``None`` -- the
    same value a 404 returns. When fetch_player used that default, every one of
    these counted as "player has no landing record": a total outage produced
    ``captured=0 no-record=3346 failed=0``, the stage exited 0, and the weekly
    job went GREEN having captured nothing.

    The previous version of this test monkeypatched fetch_player to raise
    RuntimeError -- something the real transport never does -- so it passed
    against a branch production could not reach. These go through get_json.
    """
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # don't pay the backoff
    sess = _FakeSession(*kw.get("responses", []), raises=kw.get("raises"))
    r = scrape_players(["1"], tmp_path, sleep_s=0, session=sess, log=lambda *_: None)
    assert r["failed"] == 1, "an HTTP failure must never be banked as 'no record'"
    assert r["missing"] == 0
    assert not player_path(tmp_path, 1).exists()


def test_a_small_but_complete_payload_is_captured_and_indexed(tmp_path):
    """Validity is fields, not size.

    Five real players (8472158, 8477799, 8479137, 8479155, 8483203) ship complete
    20-25 key payloads as small as 571 bytes because they have almost no career
    rows. A size floor put them in a permanent loop -- written by the sweep, read
    back as un-captured, refetched every week, and dropped from the index.
    """
    small = {"playerId": 8479155, "shootsCatches": "L"}
    sess = _FakeSession(_FakeResp(200, small))
    r = scrape_players(["8479155"], tmp_path, sleep_s=0, session=sess, log=lambda *_: None)
    assert r["captured"] == 1

    path = player_path(tmp_path, 8479155)
    assert path.stat().st_size < 600
    assert already_captured(path) is True          # the resume agrees ...
    assert write_bio_index(tmp_path, log=lambda *_: None) == 1   # ... and so does the index


def test_a_local_write_failure_is_never_reported_as_a_capture(tmp_path, monkeypatch):
    """--force over an already-valid tree must not report success on a disk error.

    The swallow exists for a LOST RENAME RACE only. When it also covered writing
    our own temp, a disk-full or permission error under --force was hidden:
    already_captured(path) was true of the OLD file, so the error vanished and
    the old file's size was returned as a fresh capture.
    """
    doc = {"playerId": 1, "shootsCatches": "R", "pad": "x" * 900}
    sess = _FakeSession(_FakeResp(200, doc))
    assert scrape_players(["1"], tmp_path, sleep_s=0, session=sess, log=lambda *_: None)["captured"] == 1

    def boom(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", boom)
    r = scrape_players(["1"], tmp_path, force=True, sleep_s=0, session=_FakeSession(_FakeResp(200, doc)),
                       log=lambda *_: None)
    assert r["captured"] == 0 and r["failed"] == 1


def test_a_truncated_roster_span_is_refused_not_silently_shrunk(monkeypatch):
    """One readable season out of ~17 is not success.

    The per-season `except: continue` cannot tell "the release has no 2009" from
    "raw.githubusercontent rate-limited us", so a transport failure yielded a
    truncated work list and a run that looked healthy while capturing a fraction
    of the players.

    Patches polars.read_parquet, NOT the `_take` closure inside the function --
    monkeypatching a closure with raising=False silently sets an unused module
    attribute, the real code runs, and the test does live DNS for 17 seasons.
    """
    import nhl_raw.players as P
    import polars as pl

    calls = {"n": 0}

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("429 rate limited")
        return pl.DataFrame({"player_id": [8478402]})

    monkeypatch.setattr(pl, "read_parquet", flaky)
    with pytest.raises(RuntimeError, match="refusing a truncated work list"):
        P.player_ids_from_rosters(
            "https://example.invalid/parquet", seasons=range(2010, 2027), log=lambda *_: None
        )
    assert calls["n"] > 1, "the patch must actually be reached"
