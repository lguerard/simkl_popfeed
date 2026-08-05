"""Popfeed-specific operations built on top of the AT Protocol client.

rkeys are deliberately computed with the exact same scheme as
jellyfin_popfeed's ``PopfeedRkeyBuilder`` (TMDb-ID-based, series TMDb ID +
season + episode number for TV). Two independent sources writing the same
deterministic rkey is what makes cross-source dedup possible without any
shared state: whichever source gets there first "owns" the record, and the
other leaves it untouched forever after.

Also tracks jellyfin_popfeed's aggregate per-show/per-season progress
records (``w.tv.{seriesId}`` / ``w.ts.{seriesId}.{season}``), so a season
gets marked complete once every one of its episodes is watched, and a
series once every season is — mirroring
``PopfeedWatchedListWriter.ReconcileSeasonMarkerAsync``/
``UpsertTvShowProgressAsync`` in jellyfin_popfeed's C# source exactly,
including the field-shape quirk where the ``tv_show`` record's identity
lives under ``identifiers.tmdbId`` (not ``tmdbTvSeriesId``, unlike every
other TV record in this scheme) — that's what the reference
implementation does, so it's what interop requires here too.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from simkl_popfeed.atproto import AtProtoClient
from simkl_popfeed.models import PopfeedIdentifiers, SimklEpisode, SimklMovie

logger = logging.getLogger(__name__)

_COLLECTION_LIST = "social.popfeed.feed.list"
_COLLECTION_LIST_ITEM = "social.popfeed.feed.listItem"
_COLLECTION_REVIEW = "social.popfeed.feed.review"

# Popfeed's actual accepted status values (confirmed against
# jellyfin_popfeed's PopfeedListItemRecord.FinishedStatus/InProgressStatus
# in its C# source — these are literal strings including the "#", not
# bare words. Writing "finished" instead of "#finished" doesn't error
# (ATProto doesn't validate token values against the lexicon on write),
# it just silently doesn't register as watched in the Popfeed app.
_STATUS_FINISHED = "#finished"
_STATUS_IN_PROGRESS = "#in_progress"

# Matches jellyfin_popfeed's default list names/types (PopfeedWatchedListWriter).
_LIST_NAMES: dict[str, str] = {
    "watched_movies": "Watched Movies",
    "watched_tv_shows": "Watched Shows",
    "recent": "Recent",
}


@dataclass
class _WatchedItem:
    """A movie or episode ready to be synced to Popfeed.

    Parameters:
        creative_work_type (str): ``"movie"`` or ``"tv_episode"``.
        list_type (str): ``"watched_movies"`` or ``"watched_tv_shows"``.
        id_key (str): Deterministic identity segment (e.g. ``"mv.603"``).
        title (str): Display title.
        identifiers (PopfeedIdentifiers): Item identifiers.
        watched_at (Optional[str]): Raw watched timestamp from Simkl.
        rating (Optional[int]): Simkl rating (1-10), if any.
    """

    creative_work_type: str
    list_type: str
    id_key: str
    title: str
    identifiers: PopfeedIdentifiers
    watched_at: Optional[str]
    rating: Optional[int]


def _movie_id_key(tmdb_id: int) -> str:
    """Return the identity segment for a movie, matching jellyfin's scheme."""
    return f"mv.{tmdb_id}"


def _episode_id_key(show_tmdb_id: int, season: int, number: int) -> str:
    """Return the identity segment for an episode, matching jellyfin's scheme."""
    return f"ep.{show_tmdb_id}.{season}.{number}"


def _watched_rkey(id_key: str) -> str:
    """Deterministic rkey for a watched-list item, shared with jellyfin_popfeed."""
    return f"w.{id_key}"


def _recent_rkey(id_key: str) -> str:
    """Deterministic rkey for a Recent-list item, shared with jellyfin_popfeed."""
    return f"r.{id_key}"


def _review_rkey(id_key: str) -> str:
    """Deterministic rkey for a review record, shared with jellyfin_popfeed."""
    return f"rv.{id_key}"


def _tv_show_rkey(show_tmdb_id: int) -> str:
    """Deterministic rkey for a show's progress record (PopfeedRkeyBuilder.ForTvShowProgress)."""
    return f"w.tv.{show_tmdb_id}"


def _tv_season_rkey(show_tmdb_id: int, season: int) -> str:
    """Deterministic rkey for a completed-season marker (PopfeedRkeyBuilder.ForTvSeason)."""
    return f"w.ts.{show_tmdb_id}.{season}"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        str: Current timestamp in ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` form.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_datetime_iso(value: Optional[str], fallback: str) -> str:
    """Normalise a Simkl datetime string to full ISO-8601, defaulting on failure.

    Parameters:
        value (Optional[str]): Source datetime string (Simkl uses full
            ISO-8601 with a ``Z`` suffix already, but this stays defensive).
        fallback (str): ISO-8601 datetime to use when ``value`` is absent
            or cannot be parsed.

    Returns:
        str: ISO-8601 datetime string ending in ``Z``.
    """
    if not value:
        return fallback
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Invalid datetime value %r; using fallback", value)
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _rating_to_popfeed(rating: Optional[int]) -> Optional[int]:
    """Map Simkl's 1-10 rating scale to Popfeed's 1-5 scale.

    Uses ceiling division so every pair of Simkl points (1-2, 3-4, ...,
    9-10) maps to one Popfeed star, and a rating of 1 (Simkl's worst)
    still maps to 1 star rather than 0 — ``round()`` would map it to 0
    via banker's rounding, which reads as "unrated" rather than "worst".

    Parameters:
        rating (Optional[int]): Simkl rating (1-10), or None.

    Returns:
        Optional[int]: 1-5 rating, or None if ``rating`` is None.
    """
    if rating is None:
        return None
    return -(-rating // 2)


def _movie_to_item(movie: SimklMovie) -> _WatchedItem:
    """Convert a SimklMovie into a source-agnostic _WatchedItem."""
    return _WatchedItem(
        creative_work_type="movie",
        list_type="watched_movies",
        id_key=_movie_id_key(movie.tmdb_id),
        title=movie.title,
        identifiers=PopfeedIdentifiers(imdb_id=movie.imdb_id, tmdb_id=movie.tmdb_id),
        watched_at=movie.watched_at,
        rating=movie.rating,
    )


def _episode_to_item(episode: SimklEpisode) -> _WatchedItem:
    """Convert a SimklEpisode into a source-agnostic _WatchedItem."""
    title = f"{episode.show_title} S{episode.season:02d}E{episode.number:02d}"
    return _WatchedItem(
        creative_work_type="tv_episode",
        list_type="watched_tv_shows",
        id_key=_episode_id_key(episode.show_tmdb_id, episode.season, episode.number),
        title=title,
        identifiers=PopfeedIdentifiers(
            imdb_id=episode.show_imdb_id,
            tmdb_tv_series_id=episode.show_tmdb_id,
            season_number=episode.season,
            episode_number=episode.number,
        ),
        watched_at=episode.watched_at,
        rating=episode.rating,
    )


class PopfeedClient:
    """High-level Popfeed operations for syncing Simkl watch history.

    Parameters:
        atproto (AtProtoClient): Authenticated AT Protocol client.
        list_names (dict[str, str]): Override mapping of list type to
            display name (falls back to jellyfin_popfeed's defaults).
        dry_run (bool): If True, log but do not write records.
    """

    def __init__(
        self,
        atproto: AtProtoClient,
        list_names: Optional[dict[str, str]] = None,
        dry_run: bool = False,
    ) -> None:
        """Initialise with an authenticated AT Protocol client."""
        self._atproto = atproto
        self._list_names = {**_LIST_NAMES, **(list_names or {})}
        self._dry_run = dry_run

    def ensure_lists(self) -> dict[str, str]:
        """Find or create the Watched Movies/Shows/Recent lists.

        Resolves lists **by name** first (same as jellyfin_popfeed), so an
        existing jellyfin_popfeed-created list is reused instead of a
        duplicate being created.

        Returns:
            dict[str, str]: Mapping of list type to AT URI.
        """
        did = self._atproto.session.did
        needed = set(self._list_names.keys())
        found: dict[str, str] = {}

        logger.info("Searching for existing Popfeed lists...")
        for record in self._atproto.iter_all_records(did, _COLLECTION_LIST):
            value: dict = record.get("value", {})
            name = value.get("name", "")
            for list_type, list_name in self._list_names.items():
                if list_type not in found and name.lower() == list_name.lower():
                    found[list_type] = record["uri"]
                    logger.info("Found %r list: %s", list_name, record["uri"])

        for list_type in needed:
            if list_type not in found:
                found[list_type] = self._create_list(did, list_type)

        return found

    def _create_list(self, did: str, list_type: str) -> str:
        """Create a Popfeed list of the given type.

        Parameters:
            did (str): The user's DID.
            list_type (str): Popfeed list type (e.g. ``"watched_movies"``).

        Returns:
            str: AT URI of the newly created list.
        """
        name = self._list_names[list_type]
        record = {
            "$type": _COLLECTION_LIST,
            "name": name,
            "listType": list_type,
            "authorDid": did,
            "createdAt": _now_iso(),
        }
        if self._dry_run:
            logger.info("[dry-run] Would create %r list", name)
            return f"at://{did}/{_COLLECTION_LIST}/dry-run-{list_type}"
        result = self._atproto.create_record(
            did=did, collection=_COLLECTION_LIST, record=record
        )
        uri: str = result["uri"]
        logger.info("Created %r list: %s", name, uri)
        return uri

    def _already_tracked(self, did: str, watched_rkey: str) -> bool:
        """Return True if a watched-list item already exists at this rkey.

        This is the entire cross-source dedup mechanism: jellyfin_popfeed
        and simkl_popfeed compute the same rkey for the same title, so
        "does a record already exist here" answers "has this already been
        correctly tracked (by either source)". Existing records are never
        read further or diffed — presence alone means skip.

        Parameters:
            did (str): The user's DID.
            watched_rkey (str): The deterministic watched-list rkey.

        Returns:
            bool: True if a record already exists at that rkey.
        """
        return (
            self._atproto.get_record(did, _COLLECTION_LIST_ITEM, watched_rkey)
            is not None
        )

    def sync_movie(self, movie: SimklMovie, list_uris: dict[str, str]) -> bool:
        """Sync a single watched movie to Popfeed, if not already tracked.

        Parameters:
            movie (SimklMovie): The watched movie to sync.
            list_uris (dict[str, str]): Mapping of list type to AT URI, as
                returned by :meth:`ensure_lists`.

        Returns:
            bool: True if a new record was written, False if skipped.
        """
        return self._sync_item(_movie_to_item(movie), list_uris)

    def sync_episode(self, episode: SimklEpisode, list_uris: dict[str, str]) -> bool:
        """Sync a single watched episode to Popfeed, if not already tracked.

        Parameters:
            episode (SimklEpisode): The watched episode to sync.
            list_uris (dict[str, str]): Mapping of list type to AT URI, as
                returned by :meth:`ensure_lists`.

        Returns:
            bool: True if a new record was written, False if skipped.
        """
        return self._sync_item(_episode_to_item(episode), list_uris)

    def _sync_item(self, item: _WatchedItem, list_uris: dict[str, str]) -> bool:
        """Write watched-list, Recent-list, and (optional) review records.

        Skips entirely if the deterministic watched-list rkey is already
        occupied — by jellyfin_popfeed or an earlier simkl_popfeed run.

        Parameters:
            item (_WatchedItem): The item to sync.
            list_uris (dict[str, str]): Mapping of list type to AT URI.

        Returns:
            bool: True if a new record was written, False if skipped.
        """
        did = self._atproto.session.did
        watched_rkey = _watched_rkey(item.id_key)

        if self._already_tracked(did, watched_rkey):
            logger.debug("Skipping %r (already tracked)", item.title)
            return False

        now = _now_iso()
        watched_at = _to_datetime_iso(item.watched_at, fallback=now)
        identifiers = item.identifiers.as_dict()

        watched_record = {
            "$type": _COLLECTION_LIST_ITEM,
            "listUri": list_uris[item.list_type],
            "listType": item.list_type,
            "creativeWorkType": item.creative_work_type,
            "identifiers": identifiers,
            "status": _STATUS_FINISHED,
            "addedAt": watched_at,
            "completedAt": watched_at,
            "title": item.title,
        }
        recent_record = {
            "$type": _COLLECTION_LIST_ITEM,
            "listUri": list_uris["recent"],
            "listType": "recent",
            "creativeWorkType": item.creative_work_type,
            "identifiers": identifiers,
            "status": _STATUS_FINISHED,
            "addedAt": watched_at,
            "completedAt": watched_at,
            "title": item.title,
        }

        if self._dry_run:
            logger.info("[dry-run] Would create watched entry for %r", item.title)
        else:
            self._atproto.put_record(
                did=did,
                collection=_COLLECTION_LIST_ITEM,
                rkey=watched_rkey,
                record=watched_record,
            )
            self._atproto.put_record(
                did=did,
                collection=_COLLECTION_LIST_ITEM,
                rkey=_recent_rkey(item.id_key),
                record=recent_record,
            )
            logger.info("Created watched entry for %r", item.title)

        self._write_review_if_present(did, item, identifiers, watched_at)
        return True

    def _write_review_if_present(
        self,
        did: str,
        item: _WatchedItem,
        identifiers: dict,
        watched_at: str,
    ) -> None:
        """Write a review record when Simkl has a rating for this item.

        Simkl's API doesn't expose review/comment text (documented but
        marked "in dev" as of this writing), so this is rating-only —
        unlike trakt_popfeed's review records, there's no ``text`` field
        to populate.

        Parameters:
            did (str): The user's DID.
            item (_WatchedItem): The item being reviewed.
            identifiers (dict): Pre-built identifiers dict for the item.
            watched_at (str): ISO-8601 watched timestamp, used as
                ``createdAt``.
        """
        popfeed_rating = _rating_to_popfeed(item.rating)
        if popfeed_rating is None:
            return

        review_record: dict = {
            "$type": _COLLECTION_REVIEW,
            "title": item.title,
            "text": "",
            "identifiers": identifiers,
            "creativeWorkType": item.creative_work_type,
            "createdAt": watched_at,
            "tags": ["simkl", "watched"],
            "rating": popfeed_rating,
        }

        if self._dry_run:
            logger.info("[dry-run] Would write review for %r", item.title)
            return
        self._atproto.put_record(
            did=did,
            collection=_COLLECTION_REVIEW,
            rkey=_review_rkey(item.id_key),
            record=review_record,
        )

    def sync_show_progress(
        self,
        episodes: list[SimklEpisode],
        season_totals: dict[int, dict[int, int]],
        list_uris: dict[str, str],
    ) -> None:
        """Mark seasons/series complete once every episode of them is watched.

        Mirrors jellyfin_popfeed's ``UpsertTvShowProgressAsync``/
        ``ReconcileSeasonMarkerAsync`` exactly: one ``tv_show`` progress
        record per series (always present, ``#in_progress`` until every
        season with known episodes is complete, then ``#finished``), and
        one ``tv_season`` record per *complete* season only — an
        incomplete season has no record at all, so a season that
        regresses (shouldn't normally happen, since nothing here removes
        watched status) has its marker deleted rather than left stale.

        Parameters:
            episodes (list[SimklEpisode]): Every currently-known-watched
                episode across all shows (not just this run's new ones —
                completion needs the full picture).
            season_totals (dict[int, dict[int, int]]): Per-show mapping of
                season number to total episode count, from
                :meth:`simkl_popfeed.tmdb.TmdbClient.get_season_episode_counts`.
                Shows missing from this dict are skipped entirely (TMDb
                lookup failed or wasn't attempted).
            list_uris (dict[str, str]): Mapping of list type to AT URI, as
                returned by :meth:`ensure_lists`.
        """
        did = self._atproto.session.did
        by_show: dict[int, list[SimklEpisode]] = {}
        for ep in episodes:
            by_show.setdefault(ep.show_tmdb_id, []).append(ep)

        for show_tmdb_id, show_episodes in by_show.items():
            totals = season_totals.get(show_tmdb_id)
            if not totals:
                continue

            show_title = show_episodes[0].show_title
            watched_by_season: dict[int, set[int]] = {}
            for ep in show_episodes:
                watched_by_season.setdefault(ep.season, set()).add(ep.number)

            season_complete = {
                season: len(watched_by_season.get(season, set())) >= total
                for season, total in totals.items()
            }
            for season in totals:
                self._sync_season_marker(
                    did, show_tmdb_id, show_title, season, season_complete[season], list_uris
                )

            series_complete = all(season_complete.values())
            self._sync_show_progress_record(
                did, show_tmdb_id, show_title, series_complete, show_episodes, list_uris
            )

    def _sync_season_marker(
        self,
        did: str,
        show_tmdb_id: int,
        show_title: str,
        season: int,
        is_complete: bool,
        list_uris: dict[str, str],
    ) -> None:
        """Create, update, or remove a single season's completion marker."""
        rkey = _tv_season_rkey(show_tmdb_id, season)

        if not is_complete:
            if not self._dry_run:
                self._atproto.delete_record(did, _COLLECTION_LIST_ITEM, rkey)
            return

        now = _now_iso()
        if self._dry_run:
            logger.info(
                "[dry-run] Would mark %r Season %d complete", show_title, season
            )
            return

        existing = self._atproto.get_record(did, _COLLECTION_LIST_ITEM, rkey)
        added_at = ((existing or {}).get("value") or {}).get("addedAt") or now
        title = f"{show_title} - Season {season:02d}" if show_title else f"Season {season:02d}"

        self._atproto.put_record(
            did=did,
            collection=_COLLECTION_LIST_ITEM,
            rkey=rkey,
            record={
                "$type": _COLLECTION_LIST_ITEM,
                "identifiers": PopfeedIdentifiers(
                    tmdb_tv_series_id=show_tmdb_id, season_number=season
                ).as_dict(),
                "creativeWorkType": "tv_season",
                "listUri": list_uris["watched_tv_shows"],
                "listType": "watched_tv_shows",
                "status": _STATUS_FINISHED,
                "addedAt": added_at,
                "completedAt": now,
                "title": title,
            },
        )
        logger.info("Marked %r Season %d complete", show_title, season)

    def _sync_show_progress_record(
        self,
        did: str,
        show_tmdb_id: int,
        show_title: str,
        is_complete: bool,
        episodes: list[SimklEpisode],
        list_uris: dict[str, str],
    ) -> None:
        """Create or update a series' overall progress record."""
        rkey = _tv_show_rkey(show_tmdb_id)
        now = _now_iso()
        if self._dry_run:
            logger.info(
                "[dry-run] Would update show progress for %r (%d episode(s) watched, complete=%s)",
                show_title,
                len(episodes),
                is_complete,
            )
            return

        existing = self._atproto.get_record(did, _COLLECTION_LIST_ITEM, rkey)
        added_at = ((existing or {}).get("value") or {}).get("addedAt") or now
        watched_episodes = [
            {"seasonNumber": ep.season, "episodeNumber": ep.number}
            for ep in sorted(episodes, key=lambda e: (e.season, e.number))
        ]

        # Matches jellyfin_popfeed's PopfeedIdentifiers { TmdbId = seriesId }
        # for tv_show records specifically — tmdbId, not tmdbTvSeriesId.
        record: dict = {
            "$type": _COLLECTION_LIST_ITEM,
            "identifiers": PopfeedIdentifiers(tmdb_id=show_tmdb_id).as_dict(),
            "creativeWorkType": "tv_show",
            "listUri": list_uris["watched_tv_shows"],
            "listType": "watched_tv_shows",
            "status": _STATUS_FINISHED if is_complete else _STATUS_IN_PROGRESS,
            "addedAt": added_at,
            "title": show_title,
            "watchedEpisodes": watched_episodes,
        }
        if is_complete:
            record["completedAt"] = now

        self._atproto.put_record(
            did=did, collection=_COLLECTION_LIST_ITEM, rkey=rkey, record=record
        )
        logger.info(
            "Updated show progress for %r (%d episode(s) watched, complete=%s)",
            show_title,
            len(episodes),
            is_complete,
        )


def read_watched_items(
    atproto: AtProtoClient,
) -> tuple[list[SimklMovie], list[SimklEpisode]]:
    """Read every watched movie/episode already on Popfeed.

    This is the other half of the bidirectional sync: it reads
    ``social.popfeed.feed.listItem`` records with a ``watched_movies``/
    ``watched_tv_shows`` ``listType`` — written either by jellyfin_popfeed
    (which runs on the Jellyfin server itself, so it needs no network
    access from here — the Jellyfin data just already being on Popfeed is
    what this bridges through) or by a previous run of this sync — and
    returns them as :class:`SimklMovie`/:class:`SimklEpisode` so
    :func:`simkl_popfeed.simkl.movies_to_history_payload`/
    :func:`simkl_popfeed.simkl.episodes_to_history_payload` can push them
    to Simkl.

    "Recent"-list copies of the same items are skipped naturally: only
    the primary watched-list record carries a ``listType`` field (the
    Recent-list record for the same item does not), so filtering on
    ``listType`` also avoids double-counting each item.

    Parameters:
        atproto (AtProtoClient): Authenticated AT Protocol client.

    Returns:
        tuple[list[SimklMovie], list[SimklEpisode]]: Movies and episodes
            found on Popfeed's watched lists.
    """
    did = atproto.session.did
    movies: list[SimklMovie] = []
    episodes: list[SimklEpisode] = []

    for record in atproto.iter_all_records(did, _COLLECTION_LIST_ITEM):
        value: dict = record.get("value", {})
        if value.get("listType") not in ("watched_movies", "watched_tv_shows"):
            continue
        identifiers: dict = value.get("identifiers", {})
        title = value.get("title", "")

        if value.get("creativeWorkType") == "movie":
            tmdb_id = identifiers.get("tmdbId")
            if not tmdb_id:
                continue
            movies.append(
                SimklMovie(
                    tmdb_id=tmdb_id, imdb_id=identifiers.get("imdbId"), title=title
                )
            )
        elif value.get("creativeWorkType") == "tv_episode":
            show_tmdb_id = identifiers.get("tmdbTvSeriesId")
            season = identifiers.get("seasonNumber")
            number = identifiers.get("episodeNumber")
            if show_tmdb_id is None or season is None or number is None:
                continue
            episodes.append(
                SimklEpisode(
                    show_tmdb_id=show_tmdb_id,
                    show_imdb_id=identifiers.get("imdbId"),
                    show_title=title,
                    season=season,
                    number=number,
                )
            )

    logger.info(
        "Read %d watched movie(s) / %d watched episode(s) from Popfeed",
        len(movies),
        len(episodes),
    )
    return movies, episodes
