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

#: A landing payload smaller than this is an error body, not a player. The
#: smallest real one measured is ~11 KB (a one-game callup with no season
#: totals); 800 bytes is far below that and above any error envelope.
MIN_BYTES = 800

#: Fields a payload must carry to count as captured. ``shootsCatches`` is the
#: reason this stage exists, so a payload without it is NOT a valid capture even
#: when the fetch returned 200 -- otherwise a partial response banks and the
#: presence-based resume never retries it.
REQUIRED = ("playerId", "shootsCatches")


def player_path(root: Path, player_id: int | str) -> Path:
    return Path(root) / "players" / f"{player_id}.json"


def already_captured(path: Path, min_bytes: int = MIN_BYTES) -> bool:
    """Presence + validity. Presence alone is not enough: an error body or a
    truncated write is a file on disk too, and a bare ``exists()`` is what let
    3,347 empty payloads block refetch in a sibling repo."""
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return all(doc.get(k) is not None for k in REQUIRED)


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
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Losing the race is fine -- the other writer produced the same bytes.
        # Clean up our temp so a killed run never leaves litter behind.
        tmp.unlink(missing_ok=True)
        if not already_captured(path):
            raise
    return path.stat().st_size


def fetch_player(player_id: int | str, *, session=None) -> Optional[dict]:
    """One landing payload, or ``None`` when the endpoint has no such player.

    Returns None rather than raising on a 404: retired/short-stint ids appear in
    old rosters and legitimately have no landing record, and one missing player
    must not abort a 3,000-player sweep.
    """
    from nhl_raw.fetch import get_json

    return get_json(LANDING.format(player_id=player_id), session=session)


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
        except Exception as exc:  # pragma: no cover - upstream state
            failed += 1
            log(f"  [{i}/{len(todo)}] {pid} FAILED {type(exc).__name__}: {exc}")
            continue
        if not doc or any(doc.get(k) is None for k in REQUIRED):
            # No landing record, or one without the field this stage exists for.
            # Nothing is written: an empty payload on disk would read as captured.
            missing += 1
            continue
        n = _write_atomic(player_path(root, pid), doc)
        done += 1
        if i % 100 == 0 or i == len(todo):
            log(f"  [{i}/{len(todo)}] {pid} ok ({n:,}B)  done={done} missing={missing} failed={failed}")
        if sleep_s:
            time.sleep(sleep_s)

    log(f"players: captured={done} no-record={missing} failed={failed}")
    return {"captured": done, "missing": missing, "failed": failed, "known": len(ids)}


def player_ids_from_rosters(roster_src: str, *, seasons: Optional[Iterable[int]] = None) -> list[str]:
    """The work list: every player id in the roster parquet, local OR remote.

    Union across seasons, so a backfill reaches exactly the players the roster
    datasets can join to -- never a blind id walk, which is how an unrelated
    scraper burned 8,400+ requests on 404s.

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
        span = seasons or range(2010, _dt.date.today().year + 2)
        found = 0
        for yr in span:
            try:
                _take(f"{base}/game_rosters_{yr}.parquet")
                found += 1
            except Exception:
                continue  # a season the release does not carry is normal
        if not found:
            raise RuntimeError(
                f"no roster parquet readable under {base!r} -- check the URL, "
                "the file naming (game_rosters_{season}.parquet), or the season span"
            )
    else:
        import glob as _glob

        for f in sorted(_glob.glob(roster_src)):
            _take(f)

    return sorted(ids, key=lambda s: (len(s), s))
