"""Per-player bio capture from ``api-web.nhle.com/v1/player/{id}/landing``.

Why this exists: the roster datasets carry ``position_code`` and nothing about
how a player shoots. ``shootsCatches`` is only on the player-landing endpoint,
and it is the one attribute that is NOT reconstructible from play-by-play --
the off-wing interaction (a left-shooting player on the right side gets a
different look at the net) needs it, and no coordinate column implies it.

Shape follows the per-game tree: one payload per entity, flat, plain JSON,
committed. ``nhl/players/{player_id}.json``.

The work list is the union of player ids seen in the committed rosters, so a
backfill covers exactly the players the datasets can join to -- not a blind id
walk, which is how the CBS incident burned 8,400+ requests on 404s.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

LANDING = "https://api-web.nhle.com/v1/player/{player_id}/landing"

#: Retained only so existing callers/tests keep importing a name; the size floor
#: is NO LONGER a validity rule. It was a third, independent statement of "is
#: this payload real", and three statements drift: the write gate checked fields
#: only, the resume checked fields AND size, so a small-but-complete payload was
#: written, then read back as un-captured, refetched every week forever, and
#: dropped from the index. A guessed 800 put five real players (8472158, 8477799,
#: 8479137, 8479155, 8483203) in exactly that loop -- their complete 20-25 key
#: payloads are as small as 571 bytes because they have almost no career rows.
#:
#: Re-measuring the floor only moved the cliff. Validity is now ONE rule, stated
#: once in :func:`_payload_is_valid`: it parses and carries :data:`REQUIRED`.
#: Truncation is already impossible three other ways -- writes are atomic
#: (tmp + rename), the JSON must parse, and the required fields must be present.
MIN_BYTES = 0

#: Fields a payload must carry to count as captured. ``shootsCatches`` is the
#: reason this stage exists, so a payload without it is NOT a valid capture even
#: when the fetch returned 200 -- otherwise a partial response banks and the
#: presence-based resume never retries it.
REQUIRED = ("playerId", "shootsCatches")


def player_path(root: Path, player_id: int | str) -> Path:
    return Path(root) / "players" / f"{player_id}.json"


def _payload_is_valid(doc: object) -> bool:
    """THE validity rule. Stated once, so the write gate, the presence-based
    resume and the bio index can never disagree about what a real payload is."""
    return isinstance(doc, dict) and all(doc.get(k) is not None for k in REQUIRED)


def already_captured(path: Path, min_bytes: int = MIN_BYTES) -> bool:
    """Presence + validity. Presence alone is not enough: an error body or a
    truncated write is a file on disk too, and a bare ``exists()`` is what let
    3,347 empty payloads block refetch in a sibling repo.

    ``min_bytes`` is vestigial (defaults to 0) -- see :data:`MIN_BYTES`.
    """
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return _payload_is_valid(doc)


def _write_atomic(path: Path, doc: dict) -> int:
    """tmp + rename, so a partial write can never occupy the real path -- which
    is what makes the presence-based resume above safe.

    The temp name carries the PID. A fixed ``.part`` looks fine until two runs
    overlap: on Windows the second `replace()` dies with
    ``PermissionError: [WinError 32] ... being used by another process`` and the
    whole sweep aborts. That is not hypothetical -- a backfill that outlived its
    session was still running when a second was started against the same tree,
    and they collided on ``8473449.json.part``. Per-process temps make the
    overlap harmless: both write, the last rename wins, and the payload is
    identical either way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    # Writing our OWN temp must always propagate: a disk-full or permission error
    # here is a local failure, never someone else's rename. Only the replace can
    # lose a race, so only the replace is allowed to be swallowed -- otherwise a
    # --force refresh over an already-valid tree reports every player captured
    # while writing nothing, because `already_captured(path)` is true of the OLD
    # file and the size returned below is the OLD file's.
    try:
        tmp.write_text(payload, encoding="utf-8")
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    try:
        tmp.replace(path)
    except OSError:
        # Losing the race is fine -- the other writer produced the same bytes.
        # Clean up our temp so a killed run never leaves litter behind.
        tmp.unlink(missing_ok=True)
        if not (path.is_file() and path.read_text(encoding="utf-8") == payload):
            raise
    return path.stat().st_size


def fetch_player(player_id: int | str, *, session=None) -> Optional[dict]:
    """One landing payload, or ``None`` when the endpoint has no such player.

    ``None`` means a 404 ONLY: retired/short-stint ids appear in old rosters and
    legitimately have no landing record, and one such player must not abort a
    3,000-player sweep.

    Every other failure -- 403, an exhausted 429/5xx budget, a connection reset,
    a timeout, an unparseable body -- raises ``FetchError`` (``strict=True``).
    That distinction is load-bearing: the transport's default collapses all of
    them to ``None``, which this function used to return, so a total outage
    counted 3,346 players as "no landing record", left ``failed`` at zero, and
    exited 0. The weekly job went green having captured nothing.
    """
    from nhl_raw.fetch import get_json

    return get_json(LANDING.format(player_id=player_id), session=session, strict=True)


def scrape_players(
    player_ids: Iterable[int | str],
    root: Path,
    *,
    limit: Optional[int] = None,
    force: bool = False,
    sleep_s: Optional[float] = None,
    session=None,
    log: Callable[[str], None] = print,
) -> dict:
    """Capture every outstanding player. Idempotent: re-running fetches nothing.

    Pace is env-only (``NHL_PLAYERS_SLEEP``, default 0.25s) so it can be retuned
    without a code change, per the repo's rate-limit convention.
    """
    root = Path(root)
    if sleep_s is None:
        sleep_s = float(os.environ.get("NHL_PLAYERS_SLEEP", "0.25"))

    ids = [str(p) for p in player_ids if p is not None and str(p).strip() not in ("", "null")]
    ids = sorted(set(ids), key=lambda s: (len(s), s))
    todo = ids if force else [p for p in ids if not already_captured(player_path(root, p))]
    if limit is not None:
        if limit < 0:
            raise ValueError(f"--limit must be >= 0, got {limit}")
        todo = todo[:limit]

    log(f"players: {len(todo):,} to capture (of {len(ids):,} known)")
    done = missing = failed = 0
    for i, pid in enumerate(todo, 1):
        try:
            doc = fetch_player(pid, session=session)
            if not _payload_is_valid(doc):
                # No landing record, or one without the field this stage exists for.
                # Nothing is written: an empty payload on disk would read as captured.
                missing += 1
                continue
            n = _write_atomic(player_path(root, pid), doc)
            done += 1
            if i % 100 == 0 or i == len(todo):
                log(f"  [{i}/{len(todo)}] {pid} ok ({n:,}B)  done={done} missing={missing} failed={failed}")
        except Exception as exc:  # pragma: no cover - upstream state
            failed += 1
            log(f"  [{i}/{len(todo)}] {pid} FAILED {type(exc).__name__}: {exc}")
        finally:
            # Pace EVERY attempt, including the ones that skipped ahead. Sleeping
            # only after a SUCCESS means a run of no-record ids or transport
            # failures hammers the endpoint completely unpaced -- exactly the
            # moment backoff matters most, and a good way to turn a transient
            # error into a rate limit.
            if sleep_s:
                time.sleep(sleep_s)

    log(f"players: captured={done} no-record={missing} failed={failed}")
    return {"captured": done, "missing": missing, "failed": failed, "known": len(ids)}


def player_ids_from_rosters(
    roster_src: str, *, seasons: Optional[Iterable[int]] = None, log: Callable[[str], None] = print
) -> list[str]:
    """The work list: every player id in the roster parquet, local OR remote.

    Union across seasons, so a backfill reaches exactly the players the roster
    datasets can join to -- never a blind id walk, which is how an unrelated
    scraper burned 8,400+ requests on 404s.

    KNOWN LIMIT, and it has bitten: the work list is bootstrapped from the
    CONSUMER's published output, so a season the consumer has not published is
    invisible here. Season 2015 was missing from the ``nhl_game_rosters`` tag, so
    29 players who appear ONLY in 2015 were never captured, and the rebuilt 2015
    came out at 99.7% handedness until they were topped up by ``--ids``. Whenever
    a season is added to that tag, re-run this stage before trusting coverage.

    Two source shapes, because CI cannot use the local one:
      * a local glob (``.../game_rosters/parquet/*.parquet``) -- dev boxes with
        the sibling checkout;
      * an http(s) BASE DIRECTORY -- a runner, where the -data repo is far too
        large to check out. Remote paths are BUILT per season, never listed:
        there is no directory listing over raw.githubusercontent, so a glob
        against a URL silently matches nothing and the sweep reports "0 known"
        while looking healthy.
    """
    import polars as pl

    ids: set[str] = set()

    def _take(frame_path: str) -> None:
        cols = pl.read_parquet(frame_path, n_rows=1).columns
        col = "player_id" if "player_id" in cols else next((c for c in cols if c.endswith("player_id")), None)
        if not col:
            return
        ids.update(str(v) for v in pl.read_parquet(frame_path, columns=[col])[col].drop_nulls().unique().to_list())

    if roster_src.startswith(("http://", "https://")):
        import datetime as _dt

        base = roster_src.rstrip("/")
        span = list(seasons or range(2010, _dt.date.today().year + 2))
        found, skipped = 0, []
        for yr in span:
            try:
                _take(f"{base}/game_rosters_{yr}.parquet")
                found += 1
            except Exception as exc:  # a season the release does not carry is normal
                skipped.append(f"{yr} ({type(exc).__name__})")
        if skipped:
            # Logged, never silent: this except cannot tell "the release has no
            # 2009" from "raw.githubusercontent rate-limited us", and a partly-read
            # span yields a truncated work list that still looks like a healthy run.
            log(f"rosters: {len(skipped)}/{len(span)} season(s) unread -- {', '.join(skipped)}")
        # One readable season out of ~17 is not success. Demand most of the span,
        # so a transport failure trips the guard instead of quietly shrinking the
        # work list to whichever seasons happened to load.
        if found < max(1, (len(span) * 2) // 3):
            raise RuntimeError(
                f"only {found}/{len(span)} roster parquet(s) readable under {base!r} -- "
                "refusing a truncated work list. Check the URL, the file naming "
                "(game_rosters_{season}.parquet), the season span, or upstream availability"
            )
    else:
        import glob as _glob

        for f in sorted(_glob.glob(roster_src)):
            _take(f)

    return sorted(ids, key=lambda s: (len(s), s))


#: One-GET bio index beside the payloads, mirroring ``nhl_schedule_master.parquet``.
#:
#: The consumer (`fastRhockey-nhl-data`) does NOT check this repo out -- it fetches
#: over raw.githubusercontent. Reading 3,000+ per-player JSONs that way would be
#: 3,000+ requests on every daily build, so the capture stage derives one compact
#: table and the consumer reads exactly one file. Rebuilt on every run, so it can
#: never drift behind the payloads that are its only source of truth.
BIO_INDEX = "nhl_player_bio.parquet"

#: index column -> landing-payload key. Deliberately narrow: this feeds a roster
#: join, not a player-profile dataset. ``shoots_catches`` is the reason it exists.
BIO_COLUMNS: dict[str, str] = {
    "player_id": "playerId",
    "shoots_catches": "shootsCatches",
    "position_code": "position",
    "height_inches": "heightInInches",
    "weight_pounds": "weightInPounds",
    "birth_date": "birthDate",
    "birth_country": "birthCountry",
}


def build_bio_index(root: Path):
    """Every captured payload as one tidy row. Empty frame still carries the schema."""
    import polars as pl

    schema = {c: (pl.Int64 if c in ("height_inches", "weight_pounds") else pl.Utf8) for c in BIO_COLUMNS}
    rows = []
    for f in sorted((Path(root) / "players").glob("*.json")):
        # The SAME rule as the write gate and the resume -- see _payload_is_valid.
        # Restating it here is what let the index publish payloads the capture
        # stage considered unusable, and drop five real players once the two
        # disagreed.
        if not already_captured(f):
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - already_captured parsed it
            continue
        rows.append({c: (str(doc[k]) if c == "player_id" else doc.get(k)) for c, k in BIO_COLUMNS.items()})
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema_overrides=schema).unique(subset=["player_id"], keep="first").sort("player_id")


def write_bio_index(root: Path, *, log: Callable[[str], None] = print) -> int:
    """Write ``<root>/nhl_player_bio.parquet``. Returns the row count."""
    df = build_bio_index(root)
    out = Path(root) / BIO_INDEX
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out, compression="zstd")
    log(f"bio index: {df.height:,} players -> {out}")
    return df.height
