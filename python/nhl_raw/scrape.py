"""NHL raw scraper driver — Python port of ``scrape_nhl_raw.R``.

Fetches the four api-web endpoints + shifts, assembles ``raw/{game_id}.json``, overlays
the enriched PBP + parsed boxscore to produce ``final/{game_id}.json`` — the input the
``fastRhockey-nhl-data`` reshaper consumes.

``build_*_from_responses`` are pure (no network) so the whole pipeline is parity-testable
against the committed fixtures; ``fetch_responses`` / ``build_*_json`` add the live fetch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from nhl_raw.assemble import assemble_raw
from nhl_raw.boxscore import parse_boxscore
from nhl_raw.feed import build_pbp, parse_game_rosters
from nhl_raw.fetch import fetch_endpoint
from nhl_raw.shifts import nhl_game_shifts

_GAME_TYPE = {1: "PR", 2: "R", 3: "P", 4: "A"}


def feed_game_info(pbp_raw: dict, game_id: int) -> list[dict]:
    """The parsed one-row game_info ``nhl_game_feed`` overlays onto ``final`` (not the raw dict)."""
    home, away = pbp_raw.get("homeTeam") or {}, pbp_raw.get("awayTeam") or {}
    venue = pbp_raw.get("venue")
    return [
        {
            "game_id": int(game_id),
            "season": pbp_raw.get("season"),
            "game_type": _GAME_TYPE.get(pbp_raw.get("gameType")),
            "game_date": pbp_raw.get("gameDate"),
            "venue": venue.get("default") if isinstance(venue, dict) else venue,
            "home_team_abbr": home.get("abbrev"),
            "away_team_abbr": away.get("abbrev"),
            "home_score": home.get("score"),
            "away_score": away.get("score"),
            "game_state": pbp_raw.get("gameState"),
        }
    ]


def build_final_from_responses(
    pbp_raw: dict,
    box_raw: dict,
    landing: dict,
    rail: dict,
    shifts: list | None,
    game_id: int,
    *,
    xg: object | None = None,
) -> dict | None:
    """Port of ``build_final_json`` — raw keys + enriched PBP/rosters/boxscore overlays."""
    if pbp_raw is None:
        return None
    final = dict(assemble_raw(pbp_raw, box_raw, landing, rail, shifts))
    pbp = build_pbp(pbp_raw, int(game_id), shifts=shifts, xg=xg)
    final["all_plays"] = pbp.to_dicts() if pbp.height else []
    final["game_info"] = feed_game_info(pbp_raw, game_id)
    final["rosters"] = parse_game_rosters(pbp_raw).to_dicts()
    box = parse_boxscore(box_raw)
    final["team_box_parsed"] = box["team_box"].to_dicts()
    final["skater_stats"] = box["skater_stats"].to_dicts()
    final["goalie_stats"] = box["goalie_stats"].to_dicts()
    return final


def fetch_responses(game_id: int, *, session: object | None = None) -> dict:
    """Fetch the four endpoints + shifts for one game (the live inputs to assembly)."""
    return {
        "pbp_raw": fetch_endpoint(game_id, "play-by-play", session=session),
        "box_raw": fetch_endpoint(game_id, "boxscore", session=session),
        "landing": fetch_endpoint(game_id, "landing", session=session),
        "rail": fetch_endpoint(game_id, "right-rail", session=session),
        "shifts": nhl_game_shifts(game_id, session=session),
    }


def build_raw_json(game_id: int, *, session: object | None = None) -> dict | None:
    r = fetch_responses(game_id, session=session)
    if r["pbp_raw"] is None:
        return None
    return assemble_raw(r["pbp_raw"], r["box_raw"], r["landing"], r["rail"], r["shifts"])


def build_final_json(game_id: int, *, xg: object | None = None, session: object | None = None) -> dict | None:
    r = fetch_responses(game_id, session=session)
    if r["pbp_raw"] is None:
        return None
    return build_final_from_responses(r["pbp_raw"], r["box_raw"], r["landing"], r["rail"], r["shifts"], game_id, xg=xg)


def _write_json(obj: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def download_game(
    game_id: int,
    *,
    out_dir: str | Path = "nhl/json",
    process: bool = True,
    xg: object | None = None,
    session: object | None = None,
) -> bool:
    """Port of ``download_game`` — write ``raw/{gid}.json`` and (if process) ``final/{gid}.json``."""
    out = Path(out_dir)
    r = fetch_responses(game_id, session=session)
    if r["pbp_raw"] is None:
        return False
    raw = assemble_raw(r["pbp_raw"], r["box_raw"], r["landing"], r["rail"], r["shifts"])
    _write_json(raw, out / "raw" / f"{game_id}.json")
    if process:
        final = build_final_from_responses(
            r["pbp_raw"], r["box_raw"], r["landing"], r["rail"], r["shifts"], game_id, xg=xg
        )
        _write_json(final, out / "final" / f"{game_id}.json")
    return True


def scrape_season(
    season: int,
    *,
    out_dir: str | Path = "nhl/json",
    xg: object | None = None,
    rescrape: bool = True,
    limit: int = 0,
    session: object | None = None,
) -> dict:
    """Port of ``scrape_nhl_raw.R``'s season loop — scrape every completed game in a season.

    Fetches the season schedule, downloads each ``game_state == 'OFF'`` game to raw + final
    JSON. ``rescrape=False`` skips games already on disk; ``limit`` caps the count.
    """
    from nhl_raw.schedule import nhl_schedule

    completed = nhl_schedule(season, session=session).filter(pl.col("game_state") == "OFF")
    final_dir = Path(out_dir) / "final"
    existing = {int(p.stem) for p in final_dir.glob("*.json")} if final_dir.exists() else set()
    ids = completed["game_id"].to_list()
    if not rescrape:
        ids = [g for g in ids if g not in existing]
    if limit:
        ids = ids[:limit]
    scraped = sum(download_game(gid, out_dir=out_dir, xg=xg, session=session) for gid in ids)
    return {"season": season, "completed": completed.height, "to_scrape": len(ids), "scraped": scraped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nhl_raw.scrape",
        description="Scrape NHL game(s) to raw + final JSON: one game (positional) or a season range (-s/-e).",
    )
    ap.add_argument("game_id", type=int, nargs="?", help="single NHL game id, e.g. 2024020001")
    ap.add_argument("-s", "--start", type=int, help="start season end-year (e.g. 2025 = 2024-25)")
    ap.add_argument("-e", "--end", type=int, help="end season end-year (default: --start)")
    ap.add_argument("--out-dir", default="nhl/json")
    ap.add_argument(
        "--models",
        default=None,
        help="dir with xg_model_{5v5,st}.json (+ meta); omit to download the canonical models on first use",
    )
    ap.add_argument("--no-xg", action="store_true", help="skip xG (no model download / computation)")
    ap.add_argument("--no-rescrape", action="store_true", help="season mode: skip games already on disk")
    ap.add_argument("--limit", type=int, default=0, help="season mode: cap games scraped (0 = all)")
    ap.add_argument("--no-process", action="store_true", help="single-game: write raw only (skip final)")
    args = ap.parse_args(argv)

    xg = None
    if not args.no_xg:
        from nhl_raw.xg import load_xg_models

        xg = load_xg_models(args.models)  # args.models=None -> download-on-first-use

    if args.game_id is not None:
        ok = download_game(args.game_id, out_dir=args.out_dir, process=not args.no_process, xg=xg)
        print(f"{'wrote' if ok else 'FAILED'} game {args.game_id} -> {args.out_dir}")
        return 0 if ok else 1

    if args.start is None:
        ap.error("provide a game_id, or -s/--start for season mode")
    for season in range(args.start, (args.end or args.start) + 1):
        summary = scrape_season(season, out_dir=args.out_dir, xg=xg, rescrape=not args.no_rescrape, limit=args.limit)
        print(f"season {season}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
