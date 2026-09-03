"""Stage 02 — NHL per-player bio capture (``shootsCatches`` and the rest).

Thin numbered entry over ``nhl_raw.players``; the package owns the logic and
this file owns operability (argparse, work-list resolution, exit code).

Writes ``nhl/players/{player_id}.json``, one payload per player, resumable and
idempotent: a player is outstanding only when its file is missing, too small, or
lacks ``shootsCatches`` -- so Ctrl-C and re-run is always safe and a redundant
run fetches nothing.

The work list is the union of player ids in the sibling ``nhl-data`` roster
parquet, so a backfill covers exactly the players the datasets can join to.
Point ``--rosters`` at that glob; ``--ids`` overrides it for a one-off.

Usage::

    python -m nhl_raw_02_players_scrape                       # full backfill
    python -m nhl_raw_02_players_scrape --limit 25            # smoke
    python -m nhl_raw_02_players_scrape --ids 8478402,8471214
    NHL_PLAYERS_SLEEP=0.1 scripts/nhl_raw.sh 02               # retune pace
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_ROSTERS = "../fastRhockey-nhl-data/nhl/game_rosters/parquet/*.parquet"


def nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def main(argv: list[str] | None = None) -> int:
    from nhl_raw.players import player_ids_from_rosters, scrape_players

    ap = argparse.ArgumentParser(prog="python -m nhl_raw_02_players_scrape")
    ap.add_argument("--root", default="nhl", help="capture root (default: nhl/)")
    ap.add_argument(
        "--rosters", default=DEFAULT_ROSTERS, help="glob of roster parquet supplying the player-id work list"
    )
    ap.add_argument("--ids", default="", help="comma-separated ids, overriding --rosters")
    ap.add_argument(
        "--limit", type=nonneg_int, default=None, help="cap this run at N players (0 means zero, not unlimited)"
    )
    ap.add_argument("--force", action="store_true", help="refetch even when already captured")
    args = ap.parse_args(argv)

    if args.ids.strip():
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    else:
        ids = player_ids_from_rosters(args.rosters)
        if not ids:
            print(
                f"FATAL: no player ids from {args.rosters!r} -- is the sibling "
                "nhl-data checkout present, or pass --ids?",
                file=sys.stderr,
            )
            return 2

    res = scrape_players(ids, Path(args.root), limit=args.limit, force=args.force)
    # A failure to FETCH is not the same as a player with no landing record:
    # exit non-zero only on the former, so a cron can tell them apart.
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
