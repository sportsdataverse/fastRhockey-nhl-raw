"""Stage 01 — NHL raw scrape (python port; raw + processed final per game).

Thin numbered entry over ``nhl_raw.scrape``; args forward verbatim. This is
the scheduled production path (``scripts/daily_nhl_scraper.sh``, droplet
cron) as of the 2026-09 R retirement -- the R scraper
(``R/scrape_nhl_raw.R``) is no longer invoked by anything scheduled.

Usage::

    python -m nhl_raw_01_scrape -s 2026 [--no-rescrape] [--no-xg]
    python -m nhl_raw_01_scrape 2024020001        # single game
    scripts/nhl_raw.sh 01
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from nhl_raw.scrape import main as _main

    argv = list(argv) if argv is not None else sys.argv[1:]
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
