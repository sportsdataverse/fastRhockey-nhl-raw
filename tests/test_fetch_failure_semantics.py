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

import polars as pl
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from nhl_raw.fetch import FetchError, get_json  # noqa: E402


class _Resp:
    def __init__(self, status, body=None, text=""):
        # `text` matters: the HTML TOI leg reads r.text, so without it the fake
        # session cannot serve a 200 whose body is not a TOI report.
        self.status_code, self._body, self.text = status, body, text

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
        if isinstance(self.default, _Resp):
            return self.default
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
    rc = scrape.main(["2024020001", "--no-xg"])
    assert rc == 1
    assert "FAILED game 2024020001" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [_Resp(503), requests.exceptions.ConnectionError("reset")], ids=["503", "reset"])
def test_a_failed_html_toi_report_is_not_no_shifts(bad):
    """Third instance of the same hole, on the last path that had it.

    _parse_toi_side returned [] for BOTH a transport failure and a report with no
    rows. When shiftcharts legitimately 404s the JSON failure flag is false, so an
    HTML 503 or connection reset flowed back as "this game has no shifts" and
    download_game persisted it permanently.
    """
    from nhl_raw import shifts

    sess = _Session(rules={"shiftcharts": _Resp(404), "htmlreports": bad})
    with pytest.raises(FetchError, match="TOI report"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_a_missing_toi_report_is_still_absent_not_a_failure():
    """404 on the TOI report keeps meaning 'no report for this game'."""
    from nhl_raw import shifts

    sess = _Session(rules={"shiftcharts": _Resp(404)}, default=_Resp(404))
    assert shifts.nhl_game_shifts(2024020001, session=sess) is None


def test_season_accounting_covers_every_outcome(tmp_path, monkeypatch):
    """scraped + failed + absent must equal to_scrape.

    download_game returns False when play-by-play 404s — genuinely absent, not
    broken. That outcome was counted in neither bucket, so the summary silently
    failed to add up and a gap was invisible. It is also NOT a failure: calling it
    one would redden the job over real 404s.
    """
    from nhl_raw import scrape

    outcomes = {1: True, 2: False, 3: FetchError("right-rail -> HTTP 503")}

    def fake_download(gid, **kw):
        r = outcomes[gid]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(scrape, "download_game", fake_download)
    # Patch it where it is LOOKED UP: scrape_season imports nhl_schedule inside the
    # function body, so patching scrape.nhl_schedule binds nothing and the real
    # fetch runs (this test hit the network until that was fixed).
    from nhl_raw import schedule as _sched

    monkeypatch.setattr(
        _sched, "nhl_schedule",
        lambda *a, **k: pl.DataFrame({"game_id": [1, 2, 3], "game_state": ["OFF"] * 3}),
    )
    s = scrape.scrape_season(2025, out_dir=tmp_path, session=None)
    assert (s["scraped"], s["absent"], s["failed"]) == (1, 1, 1)
    assert s["scraped"] + s["absent"] + s["failed"] == s["to_scrape"]


def test_a_200_that_is_not_a_toi_report_is_a_failure():
    """The FOURTH instance of the collapse, found four lines below the third.

    The host hard-404s every missing report, so a 200 without `teamHeading` is
    never "a report with no rows" -- it is a CDN/WAF error page. Returning [] for
    it banked `shifts: null` permanently, exactly like the bug above.
    """
    from nhl_raw import shifts

    junk = _Resp(200, text="<html><body>Access Denied</body></html>")
    sess = _Session(rules={"shiftcharts": _Resp(404), "htmlreports": junk})
    with pytest.raises(FetchError, match="not a TOI report"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_a_season_where_every_game_404s_is_an_outage_not_a_quiet_season(tmp_path, monkeypatch):
    """Per game a 404 is absence; for a whole season it is an outage wearing
    absence as a costume, and it used to exit 0 having written nothing."""
    from nhl_raw import schedule as _sched
    from nhl_raw import scrape

    # >= 100 ids on purpose: the guard fires only on a FULL-SEASON attempt, because a
    # handful of absent games carries no outage evidence and firing on them would
    # redden the cron forever (an absent game is never written, so it never leaves
    # the work list). See the --no-rescrape / --limit test below.
    ids = list(range(1, 1301))
    monkeypatch.setattr(scrape, "download_game", lambda gid, **kw: False)
    monkeypatch.setattr(
        _sched, "nhl_schedule",
        lambda *a, **k: pl.DataFrame({"game_id": ids, "game_state": ["OFF"] * len(ids)}),
    )
    with pytest.raises(FetchError, match="refusing to call that a quiet season"):
        scrape.scrape_season(2025, out_dir=tmp_path, session=None)


_ROW = [{"side": "H", "sweater_number": 8, "last_first": "A, B", "period": 1,
         "start_time": "0:00", "end_time": "0:45", "duration": "0:45"}]


def test_a_toi_page_with_a_heading_but_no_rows_is_drift_not_emptiness():
    """FIFTH instance. The teamHeading guard only covers a MISSING heading; a page
    that HAS one but yields no rows (an HTML re-template) returned the same [] a
    404 returns."""
    from nhl_raw import shifts

    page = _Resp(200, text='<html><td class="teamHeading">Boston Bruins</td></html>')
    sess = _Session(rules={"shiftcharts": _Resp(404), "htmlreports": page})
    with pytest.raises(FetchError, match="no shift rows parsed"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_shiftcharts_records_that_all_fail_to_normalise_is_drift_not_no_shifts(monkeypatch):
    """SIXTH instance, and on the PRIMARY leg: `data` is non-empty, so shift data
    demonstrably exists. Dropping every row is a contradiction, not an absence."""
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "_normalize_json", lambda data: pl.DataFrame())
    sess = _Session(rules={"shiftcharts": _Resp(200, {"data": [{"x": 1}]})})
    with pytest.raises(FetchError, match="none normalised"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_one_sided_toi_report_is_refused(monkeypatch):
    """The two reports are generated together upstream, so exactly one arriving is a
    flake -- and accepting it banks ONE team's shifts as the whole game."""
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "_parse_toi_side",
                        lambda season, gameno, side, sess: _ROW if side == "H" else [])
    sess = _Session(rules={"shiftcharts": _Resp(404)})
    with pytest.raises(FetchError, match="one side only"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_a_boxscore_404_after_rows_were_parsed_is_refused(monkeypatch):
    """Previously unpinned by any test. Rows parsed = the shift data exists, so
    returning None discards it and banks shifts: null permanently."""
    from nhl_raw import shifts

    monkeypatch.setattr(shifts, "_parse_toi_side", lambda *a, **k: _ROW)
    sess = _Session(rules={"shiftcharts": _Resp(404), "boxscore": _Resp(404)})
    with pytest.raises(FetchError, match="TOI shift rows parsed but the boxscore"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_rows_that_map_to_no_player_is_refused(monkeypatch):
    """Also previously unpinned. Rows in, zero mapped out is a contradiction (a
    parse_boxscore schema drift), not an absence."""
    from nhl_raw import shifts

    empty = pl.DataFrame(schema={"home_away": pl.Utf8, "sweater_number": pl.Int64,
                                 "player_id": pl.Int64, "team_id": pl.Int64, "team_abbrev": pl.Utf8})
    monkeypatch.setattr(shifts, "_parse_toi_side", lambda *a, **k: _ROW)
    monkeypatch.setattr(shifts, "parse_boxscore", lambda raw: {"skater_stats": empty, "goalie_stats": empty})
    sess = _Session(rules={"shiftcharts": _Resp(404), "boxscore": _Resp(200, {})})
    with pytest.raises(FetchError, match="none mapped to a player_id"):
        shifts.nhl_game_shifts(2024020001, session=sess)


def test_a_non_fetch_exception_is_counted_not_fatal(tmp_path, monkeypatch):
    """Previously unpinned. A ColumnNotFoundError from a schema drift, or an OSError
    on write, would otherwise abort the whole season."""
    from nhl_raw import schedule as _sched
    from nhl_raw import scrape

    def boom(gid, **kw):
        raise ValueError("schema drift")

    monkeypatch.setattr(scrape, "download_game", boom)
    monkeypatch.setattr(_sched, "nhl_schedule",
                        lambda *a, **k: pl.DataFrame({"game_id": [1, 2], "game_state": ["OFF"] * 2}))
    s = scrape.scrape_season(2025, out_dir=tmp_path, session=None)
    assert (s["failed"], s["scraped"], s["absent"]) == (2, 0, 0)


@pytest.mark.parametrize("kw", [{"rescrape": False}, {"limit": 1}], ids=["no-rescrape", "limit"])
def test_the_all_404_guard_does_not_fire_on_a_resumed_or_capped_run(tmp_path, monkeypatch, kw):
    """The guard as first written was self-reinforcing: an absent game is never
    written, so it stays in the work list, and ONE permanently-404 game would turn
    the weekly cron permanently red with no way to clear it."""
    from nhl_raw import schedule as _sched
    from nhl_raw import scrape

    monkeypatch.setattr(scrape, "download_game", lambda gid, **kw2: False)
    monkeypatch.setattr(_sched, "nhl_schedule",
                        lambda *a, **k: pl.DataFrame({"game_id": [1], "game_state": ["OFF"]}))
    s = scrape.scrape_season(2025, out_dir=tmp_path, session=None, **kw)
    assert s["absent"] == 1 and s["failed"] == 0


def test_a_schedule_where_every_team_is_absent_is_refused():
    """The likelier outage shape, and it exited 0. nhl_schedule refused all-FAILED
    but returned an empty frame for all-ABSENT, so scrape_season got ids=[], its own
    guard was skipped by the empty list, and the run went green having written
    nothing. Same host serves both endpoints."""
    from nhl_raw import schedule

    with pytest.raises(FetchError, match="refusing to report an empty season"):
        schedule.nhl_schedule(2025, teams=["BOS", "TOR"], session=_Session(default=_Resp(404)))

