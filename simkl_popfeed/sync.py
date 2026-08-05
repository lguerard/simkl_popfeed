"""Orchestrates the Simkl -> Popfeed sync."""

import logging

from simkl_popfeed.atproto import AtProtoClient
from simkl_popfeed.config import Config
from simkl_popfeed.popfeed import PopfeedClient
from simkl_popfeed.simkl import SimklClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_sync(config: Config) -> None:
    """Run the full Simkl -> Popfeed synchronisation.

    Fetches the user's full watched movie/episode history and ratings from
    Simkl, then creates a Popfeed watched-list entry (plus Recent-list
    entry and optional rating-only review) for every item that isn't
    already tracked under the same deterministic rkey — whether by
    jellyfin_popfeed or an earlier run of this sync.

    Parameters:
        config (Config): Application configuration.
    """
    if config.dry_run:
        logger.info("Dry-run mode enabled — no writes will be made.")

    with SimklClient(config.simkl_client_id, config.simkl_access_token) as simkl:
        movies = simkl.get_watched_movies()
        episodes = simkl.get_watched_episodes()
        movie_ratings = simkl.get_movie_ratings()
        episode_ratings = simkl.get_episode_ratings()

    for movie in movies:
        movie.rating = movie_ratings.get(movie.tmdb_id)

    for episode in episodes:
        key = (episode.show_tmdb_id, episode.season, episode.number)
        episode.rating = episode_ratings.get(key)

    if not movies and not episodes:
        logger.info("No watched movies or episodes found on Simkl. Exiting.")
        return

    with AtProtoClient(config.popfeed_pds_url) as atproto:
        atproto.create_session(
            identifier=config.popfeed_identifier,
            password=config.popfeed_password,
        )

        popfeed = PopfeedClient(
            atproto, list_names=config.list_names, dry_run=config.dry_run
        )
        list_uris = popfeed.ensure_lists()

        created = 0
        for movie in movies:
            try:
                if popfeed.sync_movie(movie, list_uris):
                    created += 1
            except Exception as exc:
                logger.warning("Failed to sync movie %r: %s", movie.title, exc)

        for episode in episodes:
            label = f"{episode.show_title} S{episode.season}E{episode.number}"
            try:
                if popfeed.sync_episode(episode, list_uris):
                    created += 1
            except Exception as exc:
                logger.warning("Failed to sync episode %r: %s", label, exc)

    total = len(movies) + len(episodes)
    logger.info(
        "Sync complete: %d new record(s) written, %d already tracked (%d total).",
        created,
        total - created,
        total,
    )
