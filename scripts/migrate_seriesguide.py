#!/usr/bin/env python3
"""One-time migration: SeriesGuide JSON export -> Simkl watched history.

Bypasses both the Trakt bridge (would need a personal Trakt API key) and
Simkl's Pro/VIP-gated "Import from Trakt" website tool, by reading
SeriesGuide's local JSON export directly and writing watched status via
Simkl's free developer API (``POST /sync/history``, confirmed NOT
VIP-gated). Run once, not part of the daily sync.

ponytail: SeriesGuide's export schema is documented at
https://github.com/UweTrottmann/SeriesGuide/blob/dev/docs/backup-json-schema.md
but this parser hasn't been run against a real export file yet — it tries
a couple of plausible key names for season/episode numbers
(``number``/``season``/``episode``) and prints the top-level keys of any
show/movie entry it can't parse, so a real export's actual field names can
be spotted and fixed quickly. Upgrade path: once confirmed against a real
file, drop the fallback branches and keep only the correct key names.

Usage:
    python scripts/migrate_seriesguide.py --shows-file shows-export.json \\
        --movies-file movies-export.json
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from simkl_popfeed.simkl import SimklClient, SimklError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 2.0


def _first(entry: dict, *keys: str):
    """Return the first present, non-None value among ``keys`` in ``entry``."""
    for key in keys:
        if entry.get(key) is not None:
            return entry.get(key)
    return None


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_movie_payloads(movies_export: dict) -> tuple[list[dict], int]:
    """Build Simkl /sync/history movie payloads from a SeriesGuide export.

    Returns:
        tuple[list[dict], int]: (payloads, skipped_count).
    """
    payloads: list[dict] = []
    skipped = 0
    for movie in movies_export.get("movies", []):
        if not movie.get("watched"):
            continue
        tmdb_id = _first(movie, "tmdb_id")
        if not tmdb_id:
            logger.warning(
                "Skipping movie with no tmdb_id: keys=%s", sorted(movie.keys())
            )
            skipped += 1
            continue
        payloads.append({"ids": {"tmdb": tmdb_id}, "status": "completed"})
    return payloads, skipped


def build_show_payloads(shows_export: dict) -> tuple[list[dict], int]:
    """Build Simkl /sync/history show payloads from a SeriesGuide export.

    Returns:
        tuple[list[dict], int]: (payloads, skipped_episode_count).
    """
    payloads: list[dict] = []
    skipped = 0
    for show in shows_export.get("shows", []):
        show_tmdb_id = _first(show, "tmdb_id")
        if not show_tmdb_id:
            logger.warning(
                "Skipping show with no tmdb_id: %r", show.get("title", "?")
            )
            continue

        seasons_out: dict[int, list[dict]] = {}
        for season in show.get("seasons", []):
            season_number = _first(season, "season", "number")
            for ep in season.get("episodes", []):
                if not ep.get("watched"):
                    continue
                episode_number = _first(ep, "episode", "number")
                if season_number is None or episode_number is None:
                    logger.warning(
                        "Skipping episode with no season/number in %r: keys=%s",
                        show.get("title", "?"),
                        sorted(ep.keys()),
                    )
                    skipped += 1
                    continue
                seasons_out.setdefault(season_number, []).append(
                    {"number": episode_number}
                )

        if seasons_out:
            payloads.append(
                {
                    "ids": {"tmdb": show_tmdb_id},
                    "seasons": [
                        {"number": num, "episodes": eps}
                        for num, eps in sorted(seasons_out.items())
                    ],
                }
            )
    return payloads, skipped


def _send_in_batches(client: SimklClient, key: str, items: list[dict], dry_run: bool) -> None:
    """POST ``items`` to /sync/history in batches of BATCH_SIZE, sequentially."""
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        if dry_run:
            logger.info("[dry-run] Would send %d %s", len(batch), key)
            continue
        kwargs = {"movies": batch, "shows": []} if key == "movies" else {"movies": [], "shows": batch}
        result = client.add_to_history(**kwargs)
        logger.info("Batch result: %s", result.get("added"))
        not_found = result.get("not_found", {})
        if any(not_found.values()):
            logger.warning("Some items not found by Simkl: %s", not_found)
        if i + BATCH_SIZE < len(items):
            time.sleep(BATCH_DELAY_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shows-file", help="Path to SeriesGuide shows JSON export")
    parser.add_argument("--movies-file", help="Path to SeriesGuide movies JSON export")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    if not args.shows_file and not args.movies_file:
        parser.error("at least one of --shows-file / --movies-file is required")

    load_dotenv(dotenv_path=args.env_file)
    client_id = os.environ.get("SIMKL_CLIENT_ID", "").strip()
    access_token = os.environ.get("SIMKL_ACCESS_TOKEN", "").strip()
    if not client_id or not access_token:
        print(
            "SIMKL_CLIENT_ID and SIMKL_ACCESS_TOKEN must be set (run "
            "`simkl-popfeed --setup` first).",
            file=sys.stderr,
        )
        sys.exit(1)

    movie_payloads: list[dict] = []
    show_payloads: list[dict] = []
    skipped = 0

    if args.movies_file:
        movie_payloads, movie_skipped = build_movie_payloads(_load(args.movies_file))
        skipped += movie_skipped
        logger.info("%d watched movies to migrate", len(movie_payloads))

    if args.shows_file:
        show_payloads, show_skipped = build_show_payloads(_load(args.shows_file))
        skipped += show_skipped
        logger.info("%d shows with watched episodes to migrate", len(show_payloads))

    if skipped:
        logger.warning("%d items skipped due to missing tmdb_id/season/number", skipped)

    with SimklClient(client_id, access_token) as client:
        try:
            _send_in_batches(client, "movies", movie_payloads, args.dry_run)
            _send_in_batches(client, "shows", show_payloads, args.dry_run)
        except SimklError as exc:
            print(f"Simkl API error: {exc}", file=sys.stderr)
            sys.exit(1)

    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
