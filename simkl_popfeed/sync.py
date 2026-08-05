"""Orchestrates the bidirectional Popfeed <-> Simkl sync."""

import logging

from simkl_popfeed.atproto import AtProtoClient
from simkl_popfeed.config import Config
from simkl_popfeed.popfeed import PopfeedClient, read_watched_items
from simkl_popfeed.simkl import (
    SimklClient,
    SimklError,
    episodes_to_history_payload,
    movies_to_history_payload,
    send_in_batches,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _push_popfeed_to_simkl(config: Config, simkl: SimklClient, atproto: AtProtoClient) -> None:
    """Push everything already watched on Popfeed to Simkl.

    This is what makes the sync bidirectional: Popfeed already has
    everything jellyfin_popfeed wrote directly (jellyfin_popfeed runs on
    the Jellyfin server itself, so it needs no network access to Jellyfin
    from here), plus anything a previous run of this sync wrote from
    Simkl. Pushing all of it to Simkl via ``add_to_history`` is safe to
    repeat every run — the endpoint is idempotent.

    Best-effort: a failure here is logged and doesn't stop the Simkl ->
    Popfeed half of the sync below.

    Parameters:
        config (Config): Application configuration.
        simkl (SimklClient): Authenticated Simkl client to push through.
        atproto (AtProtoClient): Authenticated AT Protocol client to read
            Popfeed's watched lists from.
    """
    try:
        movies, episodes = read_watched_items(atproto)
        movie_payload = movies_to_history_payload(movies)
        show_payload = episodes_to_history_payload(episodes)
        send_in_batches(simkl.add_to_history, "movies", movie_payload, config.dry_run)
        send_in_batches(simkl.add_to_history, "shows", show_payload, config.dry_run)
        logger.info(
            "Pushed %d movie(s) / %d show(s) from Popfeed to Simkl.",
            len(movie_payload),
            len(show_payload),
        )
    except SimklError as exc:
        logger.warning("Popfeed -> Simkl push failed, continuing: %s", exc)


def run_sync(config: Config) -> None:
    """Run the full bidirectional sync.

    First pushes everything already watched on Popfeed to Simkl
    (idempotent — safe to repeat every run; best-effort — a failure here
    doesn't stop the rest of the sync). Then fetches the user's full
    watched movie/episode history and ratings from Simkl, and creates a
    Popfeed watched-list entry (plus Recent-list entry and optional
    rating-only review) for every item that isn't already tracked under
    the same deterministic rkey — whether by jellyfin_popfeed or an
    earlier run of this sync.

    Parameters:
        config (Config): Application configuration.
    """
    if config.dry_run:
        logger.info("Dry-run mode enabled — no writes will be made.")

    with AtProtoClient(config.popfeed_pds_url) as atproto:
        atproto.create_session(
            identifier=config.popfeed_identifier,
            password=config.popfeed_password,
        )

        with SimklClient(config.simkl_client_id, config.simkl_access_token) as simkl:
            _push_popfeed_to_simkl(config, simkl, atproto)

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
