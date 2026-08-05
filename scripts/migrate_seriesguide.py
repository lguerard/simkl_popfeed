#!/usr/bin/env python3
"""One-time migration: SeriesGuide JSON export -> Simkl watched history.

Bypasses both the Trakt bridge (would need a personal Trakt API key) and
Simkl's Pro/VIP-gated "Import from Trakt" website tool, by reading
SeriesGuide's local JSON export directly and writing watched status via
Simkl's free developer API (``POST /sync/history``, confirmed NOT
VIP-gated). Run once, not part of the daily sync.

Also handles SeriesGuide's custom Lists export, best-effort: Simkl's API
has no custom-list endpoint at all (confirmed absent from its OpenAPI
spec), so list items are placed on Simkl's "plan to watch" watchlist
status instead via ``/sync/add-to-list`` — the closest available
equivalent. This loses the original list name/grouping. Items already
covered by --movies-file/--shows-file's watched data are excluded from
this step so a completed show doesn't get downgraded back to plan-to-watch
(Simkl's own docs warn against calling add-to-list on items already sent
to /sync/history in the same run).

Verified against real SeriesGuide export files (More -> Export and Import):
each file's top level is a bare JSON array (not wrapped in a
``{"movies": [...]}``-style object), of movie/show/list objects using
``tmdb_id``/``watched``/``season``/``episode`` field names directly.

Usage:
    python scripts/migrate_seriesguide.py --shows-file shows-export.json \\
        --movies-file movies-export.json --lists-file lists-export.json
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from simkl_popfeed.simkl import SimklClient, SimklError, send_in_batches  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 2.0


def _load(path: str) -> list[dict]:
    """Load a SeriesGuide JSON export file (a bare top-level array)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_movie_payloads(movies_export: list[dict]) -> tuple[list[dict], int]:
    """Build Simkl /sync/history movie payloads from a SeriesGuide export.

    Parameters:
        movies_export (list[dict]): Parsed SeriesGuide movies JSON export
            (a bare top-level array of movie objects).

    Returns:
        tuple[list[dict], int]: (payloads, skipped_count).
    """
    payloads: list[dict] = []
    skipped = 0
    for movie in movies_export:
        if not movie.get("watched"):
            continue
        tmdb_id = movie.get("tmdb_id")
        if not tmdb_id:
            logger.warning(
                "Skipping movie with no tmdb_id: keys=%s", sorted(movie.keys())
            )
            skipped += 1
            continue
        payloads.append({"ids": {"tmdb": tmdb_id}, "status": "completed"})
    return payloads, skipped


def build_show_payloads(shows_export: list[dict]) -> tuple[list[dict], int]:
    """Build Simkl /sync/history show payloads from a SeriesGuide export.

    Parameters:
        shows_export (list[dict]): Parsed SeriesGuide shows JSON export (a
            bare top-level array of show objects, each with nested
            ``seasons[].episodes[]``).

    Returns:
        tuple[list[dict], int]: (payloads, skipped_episode_count).
    """
    payloads: list[dict] = []
    skipped = 0
    for show in shows_export:
        show_tmdb_id = show.get("tmdb_id")
        if not show_tmdb_id:
            logger.warning(
                "Skipping show with no tmdb_id: %r", show.get("title", "?")
            )
            continue

        seasons_out: dict[int, list[dict]] = {}
        for season in show.get("seasons", []):
            season_number = season.get("season")
            for ep in season.get("episodes", []):
                if not ep.get("watched"):
                    continue
                episode_number = ep.get("episode")
                if season_number is None or episode_number is None:
                    logger.warning(
                        "Skipping episode with no season/episode number in %r: keys=%s",
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


def build_list_payloads(
    lists_export: list[dict],
    exclude_show_tmdb_ids: set[int],
) -> tuple[list[dict], list[dict], int]:
    """Build Simkl /sync/add-to-list payloads from a SeriesGuide lists export.

    Simkl has no custom-list API, so every item is placed on the
    "plantowatch" watchlist status instead — the list name/grouping
    itself is not preserved. Shows already covered by --shows-file are
    skipped here so they aren't downgraded back to plan-to-watch after
    already being marked watched/completed (Simkl's own docs warn against
    calling add-to-list on an item already sent to /sync/history).

    Movies aren't cross-referenced against --movies-file: SeriesGuide list
    items of type "imdb-movie" only carry an IMDb id, and the movie
    watched-history payloads are keyed on TMDb id, so there's no shared
    key to exclude on. Ceiling worth knowing about: a movie both watched
    and listed could get a redundant plantowatch call after already being
    marked completed — untested against Simkl's real conflict-resolution
    behavior for that specific case.

    Parameters:
        lists_export (list[dict]): Parsed SeriesGuide lists JSON export (a
            bare top-level array of list objects, each with an
            ``items[]`` array).
        exclude_show_tmdb_ids (set[int]): TMDb show IDs already handled as
            watched history.

    Returns:
        tuple[list[dict], list[dict], int]: (movie_payloads,
            show_payloads, skipped_count).
    """
    movie_payloads: list[dict] = []
    show_payloads: list[dict] = []
    skipped = 0

    for lst in lists_export:
        for item in lst.get("items", []):
            item_type = item.get("type")
            external_id = item.get("externalId")
            tvdb_id = item.get("tvdb_id")

            if item_type == "imdb-movie" and external_id:
                # externalId is an IMDb id string (e.g. "tt1375666") for
                # this type; Simkl accepts imdb directly, no tmdb needed.
                movie_payloads.append(
                    {"to": "plantowatch", "ids": {"imdb": external_id}}
                )
            elif item_type == "tmdb-show" and external_id:
                try:
                    tmdb_id = int(external_id)
                except (TypeError, ValueError):
                    logger.warning("Skipping show with non-numeric tmdb id: %r", external_id)
                    skipped += 1
                    continue
                if tmdb_id in exclude_show_tmdb_ids:
                    continue
                show_payloads.append(
                    {"to": "plantowatch", "ids": {"tmdb": tmdb_id}}
                )
            elif item_type == "show" and tvdb_id:
                show_payloads.append(
                    {"to": "plantowatch", "ids": {"tvdb": tvdb_id}}
                )
            else:
                # "episode"/"season" list items, or an unrecognised type —
                # no clean equivalent on Simkl's whole-show watchlist.
                logger.warning(
                    "Skipping list item with unsupported type %r: %s",
                    item_type,
                    item.get("list_item_id", "?"),
                )
                skipped += 1
                continue

    return movie_payloads, show_payloads, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shows-file", help="Path to SeriesGuide shows JSON export")
    parser.add_argument("--movies-file", help="Path to SeriesGuide movies JSON export")
    parser.add_argument("--lists-file", help="Path to SeriesGuide lists JSON export")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    if not args.shows_file and not args.movies_file and not args.lists_file:
        parser.error(
            "at least one of --shows-file / --movies-file / --lists-file is required"
        )

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

    list_movie_payloads: list[dict] = []
    list_show_payloads: list[dict] = []
    if args.lists_file:
        already_watched_show_ids = {p["ids"]["tmdb"] for p in show_payloads}
        list_movie_payloads, list_show_payloads, list_skipped = build_list_payloads(
            _load(args.lists_file), already_watched_show_ids
        )
        skipped += list_skipped
        logger.info(
            "%d list movies / %d list shows to add as plan-to-watch",
            len(list_movie_payloads),
            len(list_show_payloads),
        )

    if skipped:
        logger.warning("%d items skipped due to missing tmdb_id/season/number", skipped)

    with SimklClient(client_id, access_token) as client:
        try:
            send_in_batches(
                client.add_to_history, "movies", movie_payloads, args.dry_run,
                batch_size=BATCH_SIZE, delay_seconds=BATCH_DELAY_SECONDS,
            )
            send_in_batches(
                client.add_to_history, "shows", show_payloads, args.dry_run,
                batch_size=BATCH_SIZE, delay_seconds=BATCH_DELAY_SECONDS,
            )
            send_in_batches(
                client.add_to_watchlist, "movies", list_movie_payloads, args.dry_run,
                batch_size=BATCH_SIZE, delay_seconds=BATCH_DELAY_SECONDS,
            )
            send_in_batches(
                client.add_to_watchlist, "shows", list_show_payloads, args.dry_run,
                batch_size=BATCH_SIZE, delay_seconds=BATCH_DELAY_SECONDS,
            )
        except SimklError as exc:
            print(f"Simkl API error: {exc}", file=sys.stderr)
            sys.exit(1)

    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
