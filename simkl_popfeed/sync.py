"""Orchestrates the bidirectional Popfeed <-> Simkl sync."""

import logging

from simkl_popfeed.atproto import AtProtoClient
from simkl_popfeed.config import Config
from simkl_popfeed.models import SimklEpisode, SimklMovie
from simkl_popfeed.popfeed import PopfeedClient, read_watched_items
from simkl_popfeed.simkl import (
    SimklClient,
    SimklError,
    episodes_to_history_payload,
    movies_to_history_payload,
    send_in_batches,
)
from simkl_popfeed.tmdb import TmdbClient, TmdbError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _push_popfeed_to_simkl(
    config: Config,
    simkl: SimklClient,
    movies: list[SimklMovie],
    episodes: list[SimklEpisode],
) -> None:
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
        movies (list[SimklMovie]): Movies already on Popfeed.
        episodes (list[SimklEpisode]): Episodes already on Popfeed.
    """
    try:
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


def _merge_unique_episodes(*episode_lists: list[SimklEpisode]) -> list[SimklEpisode]:
    """Combine episode lists, deduplicating by (show, season, number).

    Parameters:
        *episode_lists (list[SimklEpisode]): Episode lists to merge.

    Returns:
        list[SimklEpisode]: One entry per unique episode.
    """
    seen: set[tuple[int, int, int]] = set()
    merged: list[SimklEpisode] = []
    for episodes in episode_lists:
        for ep in episodes:
            key = (ep.show_tmdb_id, ep.season, ep.number)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ep)
    return merged


def _sync_show_progress(
    config: Config,
    popfeed: PopfeedClient,
    all_episodes: list[SimklEpisode],
    list_uris: dict[str, str],
) -> None:
    """Mark seasons/series complete on Popfeed, if TMDb is configured.

    Looks up each watched show's season/episode totals from TMDb (one
    call per unique show — neither Simkl nor Popfeed expose totals, only
    what's been watched) and hands them to
    :meth:`PopfeedClient.sync_show_progress`. Best-effort per-show: a
    TMDb lookup failure for one show is logged and skipped, the rest
    still proceed.

    Parameters:
        config (Config): Application configuration.
        popfeed (PopfeedClient): Popfeed client to write completion
            markers through.
        all_episodes (list[SimklEpisode]): Every currently-known-watched
            episode (existing Popfeed state plus this run's Simkl reads).
        list_uris (dict[str, str]): Mapping of list type to AT URI.
    """
    if not config.tmdb_api_key:
        logger.info(
            "TMDB_API_KEY not set — skipping season/series completion marking."
        )
        return
    if not all_episodes:
        return

    show_ids = {ep.show_tmdb_id for ep in all_episodes}
    season_totals: dict[int, dict[int, int]] = {}
    with TmdbClient(config.tmdb_api_key) as tmdb:
        for show_id in show_ids:
            try:
                season_totals[show_id] = tmdb.get_season_episode_counts(show_id)
            except TmdbError as exc:
                logger.warning("Could not fetch season info for show %d: %s", show_id, exc)

    popfeed.sync_show_progress(all_episodes, season_totals, list_uris)


def run_sync(config: Config) -> None:
    """Run the full bidirectional sync.

    First pushes everything already watched on Popfeed to Simkl
    (idempotent — safe to repeat every run; best-effort — a failure here
    doesn't stop the rest of the sync). Then fetches the user's full
    watched movie/episode history and ratings from Simkl, and creates a
    Popfeed watched-list entry (plus Recent-list entry and optional
    rating-only review) for every item that isn't already tracked under
    the same deterministic rkey — whether by jellyfin_popfeed or an
    earlier run of this sync. Finally, if TMDb is configured, marks
    seasons/series complete on Popfeed once every episode of them is
    watched.

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

        popfeed_movies, popfeed_episodes = read_watched_items(atproto)

        with SimklClient(config.simkl_client_id, config.simkl_access_token) as simkl:
            _push_popfeed_to_simkl(config, simkl, popfeed_movies, popfeed_episodes)

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

        all_episodes = _merge_unique_episodes(popfeed_episodes, episodes)
        try:
            _sync_show_progress(config, popfeed, all_episodes, list_uris)
        except Exception as exc:
            logger.warning("Season/series completion pass failed: %s", exc)

    total = len(movies) + len(episodes)
    logger.info(
        "Sync complete: %d new record(s) written, %d already tracked (%d total).",
        created,
        total - created,
        total,
    )
