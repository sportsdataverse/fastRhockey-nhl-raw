"""A failed fetch is not an absent record — across every stage that banks a None.

`get_json` returns None for a 404 AND for a 403, an exhausted 429/5xx budget, a
connection reset, a timeout and an unparseable body. Any caller that writes that
None to disk, or uses it to decide there is nothing to do, cannot tell an outage
from a quiet day. These pin the distinction at each such call site.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from nhl_raw.fetch import FetchError, get_json  # noqa: E402


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Session:
    """Per-URL canned responses; anything unmatched 200s with `default`."""

    def __init__(self, rules=None, default=None, raises=None):
        self.rules, self.default, self.raises, self.urls = rules or {}, default, raises, []

    def get(self, url, **kw):
        self.urls.append(url)
        if self.raises:
            raise self.raises
        for frag, resp in self.rules.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(200, self.default)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


# --------------------------------------------------------------- transport ---
def test_strict_separates_a_404_from_every_other_outcome():
    """404 alone keeps meaning 'no such record'; everything else raises."""
    assert get_json("https://x/y", session=_Session(rules={"y": _Resp(404)}), strict=True) is None
    for resp in (_Resp(403), _Resp(503), _Resp(500)):
        with pytest.raises(FetchError):
            get_json("https://x/y", session=_Session(rules={"y": resp}), strict=True)
    with pytest.raises(FetchError):
        get_json("https://x/y", session=_Session(raises=requests.exceptions.ConnectionError("reset")), strict=True)


def test_the_default_is_unchanged_for_existing_callers():
    """strict is opt-in: the scraper's other call sites keep None-on-everything."""
    for resp in (_Resp(404), _Resp(403), _Resp(503)):
        assert get_json("https://x/y", session=_Session(rules={"y": resp})) is None


# ---------------------------------------------------------------- schedule ---
def test_a_failed_club_schedule_refuses_rather_than_returning_a_partial_season():
    """nhl_schedule decides which games get scraped. Returning it short means those
    teams' games are never fetched and the run still reports success."""
    from nhl_raw import schedule

    sess = _Session(rules={"/BOS/": _Resp(503)}, default={"games": [], "id": 1})
    with pytest.raises(FetchError, match="Refusing to return a partial schedule"):
        schedule.nhl_schedule(2025, session=sess)


def test_a_team_that_did_not_exist_yet_is_absent_not_failed(capsys):
    """_TEAMS is the current 32, so a 404 for SEA in 2015 is expected. Reporting it
    as a failure would drown the real failures in noise."""
    from nhl_raw import schedule

    sess = _Session(rules={"/SEA/": _Resp(404)}, default={"games": []})
    out = schedule.nhl_schedule(2015, teams=["SEA", "BOS"], session=sess)
    assert out.height == 0
    assert "expected pre-expansion" in capsys.readouterr().err


# ------------------------------------------------------------------ shifts ---
def test_a_failed_shiftchart_never_reads_as_a_game_with_no_shifts(monkeypatch):
    """download_game writes `shifts: null` and the resume is presence-based, so a
    transient blip would bake a permanently shift-less game into the raw store."""
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "parse_toi_html", lambda *a, **k: None)
    with pytest.raises(FetchError, match="HTML TOI fallback was empty"):
        shifts.nhl_game_shifts(2024020001, session=_Session(rules={"shiftcharts": _Resp(503)}))


def test_a_game_that_genuinely_has_no_shifts_still_returns_none(monkeypatch):
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "parse_toi_html", lambda *a, **k: None)
    assert shifts.nhl_game_shifts(2024020001, session=_Session(rules={"shiftcharts": _Resp(404)})) is None


# ------------------------------------------------------------------ scrape ---
def test_a_component_failure_writes_nothing_and_is_counted(tmp_path, monkeypatch):
    """The resume skips games already on disk, so a game written with a failed
    component is never repaired. Nothing may be written unless every component
    either arrived or is genuinely absent."""
    from nhl_raw import scrape

    monkeypatch.setattr(scrape, "nhl_game_shifts", lambda *a, **k: None)
    sess = _Session(rules={"right-rail": _Resp(503)}, default={"plays": []})
    with pytest.raises(FetchError):
        scrape.download_game(2024020001, out_dir=tmp_path, process=False, session=sess)
    assert not (Path(tmp_path) / "raw" / "2024020001.json").exists()


def test_a_boxscore_failure_inside_the_html_fallback_is_not_no_shifts(monkeypatch):
    """The narrow gap the first pass left open.

    When shiftcharts legitimately 404s, the HTML TOI fallback runs and needs the
    boxscore to map sweater numbers to player ids. With that fetch non-strict, a
    transient boxscore failure returned None from parse_toi_html; nhl_game_shifts
    saw fetch_failed=False (the 404 was legitimate) and returned None; and
    download_game persisted a permanently shift-less game.
    """
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "_parse_toi_side", lambda *a, **k: [{"last_first": "A, B"}])
    sess = _Session(rules={"shiftcharts": _Resp(404), "boxscore": _Resp(503)})
    with pytest.raises(FetchError):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_single_game_cli_reports_a_failure_instead_of_a_traceback(capsys, monkeypatch):
    """download_game raises now, so the CLI must translate that into its FAILED
    line and exit 1 rather than dumping a stack trace at an operator."""
    from nhl_raw import scrape

    def boom(*a, **k):
        raise FetchError("right-rail -> HTTP 503")

    monkeypatch.setattr(scrape, "download_game", boom)
    monkeypatch.setattr(scrape, "load_xg_models", lambda *a, **k: None, raising=False)
    rc = scrape.main(["2024020001", "--no-xg"])
    assert rc == 1
    assert "FAILED game 2024020001" in capsys.readouterr().err

