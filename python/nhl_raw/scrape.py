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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nhl_raw.scrape", description="Scrape one NHL game to raw + final JSON."
    )
    ap.add_argument("game_id", type=int, help="NHL game id, e.g. 2024020001")
    ap.add_argument("--out-dir", default="nhl/json")
    ap.add_argument("--models", default=None, help="dir with xg_model_{5v5,st}.json (+ meta) for xG")
    ap.add_argument("--no-process", action="store_true", help="write raw only (skip final/enrichment)")
    args = ap.parse_args(argv)
    xg = None
    if args.models:
        from nhl_raw.xg import load_xg_models

        xg = load_xg_models(args.models)
    ok = download_game(args.game_id, out_dir=args.out_dir, process=not args.no_process, xg=xg)
    print(f"{'wrote' if ok else 'FAILED'} game {args.game_id} -> {args.out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
