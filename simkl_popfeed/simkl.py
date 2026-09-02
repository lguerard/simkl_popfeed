"""Simkl API client.

Every request needs ``client_id``, ``app-name``, and ``app-version`` as URL
query parameters plus a ``User-Agent`` header (Simkl's own convention,
distinct from Trakt's header-based ``trakt-api-key``). Authenticated calls
add an ``Authorization: Bearer <token>`` header. Auth uses the PIN/device
code flow (:meth:`SimklClient.request_pin` / :meth:`SimklClient.poll_pin`),
which yields a long-lived (~5 year) access token — no refresh-token
rotation needed for an unattended daily cron.
"""

import logging
import time
from typing import Any, Optional

import httpx

from simkl_popfeed.models import SimklEpisode, SimklMovie

logger = logging.getLogger(__name__)

SIMKL_API_URL = "https://api.simkl.com"
APP_NAME = "simkl-popfeed"
APP_VERSION = "1.0"


class SimklError(Exception):
    """Raised when the Simkl API returns an error."""


class SimklAuthPending(Exception):
    """Raised by poll_pin while the user hasn't approved the PIN yet."""


class SimklClient:
    """Client for the Simkl API.

    Parameters:
        client_id (str): Simkl API application client ID.
        access_token (Optional[str]): User access token from the PIN flow.
            Required for every method except :meth:`request_pin` /
            :meth:`poll_pin`.
    """

    def __init__(self, client_id: str, access_token: Optional[str] = None) -> None:
        """Initialise the client.

        Parameters:
            client_id (str): Simkl API application client ID.
            access_token (Optional[str]): User access token, if already
                obtained via the PIN flow.
        """
        self._client_id = client_id
        self._access_token = access_token
        self._anime_cache: Optional[list[dict]] = None
        self._http = httpx.Client(
            base_url=SIMKL_API_URL,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
            timeout=30.0,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "SimklClient":
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close the HTTP client on context exit."""
        self.close()

    def _base_params(self) -> dict[str, str]:
        """Return the three query params every Simkl request needs."""
        return {
            "client_id": self._client_id,
            "app-name": APP_NAME,
            "app-version": APP_VERSION,
        }

    def _auth_headers(self) -> dict[str, str]:
        """Return the Authorization header for an authenticated request.

        Raises:
            SimklError: If no access token was provided.
        """
        if not self._access_token:
            raise SimklError("Not authenticated; no access_token provided")
        return {"Authorization": f"Bearer {self._access_token}"}

    def request_pin(self) -> dict:
        """Start the PIN/device-code auth flow.

        Returns:
            dict: Response with ``user_code``, ``verification_url``, and
                polling ``interval``/``expires_in`` (seconds).

        Raises:
            SimklError: On request failure.
        """
        try:
            response = self._http.get("/oauth/pin", params=self._base_params())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SimklError(f"HTTP {exc.response.status_code} requesting PIN") from exc
        except httpx.RequestError as exc:
            raise SimklError(f"PIN request failed: {exc}") from exc
        return response.json()

    def poll_pin(self, user_code: str) -> str:
        """Poll once for the access token tied to a PIN user code.

        Parameters:
            user_code (str): The ``user_code`` from :meth:`request_pin`.

        Returns:
            str: The access token, once approved.

        Raises:
            SimklAuthPending: The user hasn't approved the PIN yet; call
                again after the flow's ``interval``.
            SimklError: The PIN expired, was denied, or the request failed.
        """
        try:
            response = self._http.get(
                f"/oauth/pin/{user_code}", params=self._base_params()
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SimklError(f"HTTP {exc.response.status_code} polling PIN") from exc
        except httpx.RequestError as exc:
            raise SimklError(f"PIN poll failed: {exc}") from exc
        data: dict = response.json()
        result = data.get("result")
        if result == "OK" and data.get("access_token"):
            return data["access_token"]
        # Simkl returns result: "KO" with message "Authorization pending"
        # as the normal "not approved yet" response while polling, not a
        # real failure — only treat KO as fatal once the message says
        # otherwise (e.g. the PIN expired or the user denied it).
        if result == "KO":
            message = str(data.get("message", "")).lower()
            if "pending" in message:
                raise SimklAuthPending()
            raise SimklError(f"PIN flow failed: {data}")
        raise SimklAuthPending()

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """Issue an authenticated GET and return the parsed JSON body.

        Parameters:
            path (str): API path, e.g. ``/sync/all-items/movies/completed``.
            params (Optional[dict]): Extra query params beyond the base
                client_id/app-name/app-version trio.

        Returns:
            Any: Parsed JSON response.

        Raises:
            SimklError: On HTTP or request failure.
        """
        try:
            response = self._http.get(
                path,
                params={**self._base_params(), **(params or {})},
                headers=self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SimklError(f"HTTP {exc.response.status_code} from Simkl API: {path}") from exc
        except httpx.RequestError as exc:
            raise SimklError(f"Request to Simkl API failed: {exc}") from exc
        return response.json()

    def add_to_history(self, movies: list[dict], shows: list[dict]) -> dict:
        """POST a batch of watched items to ``/sync/history``.

        Used by the one-time SeriesGuide migration script, not by the
        daily sync (which only reads). Callers should batch ~50 items per
        call and send sequentially — Simkl enforces a 20-second per-user
        write lock and rejects concurrent writes.

        Parameters:
            movies (list[dict]): Movie entries, each at minimum
                ``{"ids": {"tmdb": ...}, "watched_at": "...ISO-8601..."}``.
            shows (list[dict]): Show entries with nested
                ``seasons: [{"number": N, "episodes": [{"number": N,
                "watched_at": "..."}]}]``.

        Returns:
            dict: Simkl's response, including ``added``/``not_found`` counts.

        Raises:
            SimklError: On HTTP or request failure.
        """
        body: dict = {}
        if movies:
            body["movies"] = movies
        if shows:
            body["shows"] = shows
        try:
            response = self._http.post(
                "/sync/history",
                params=self._base_params(),
                json=body,
                headers=self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SimklError(
                f"HTTP {exc.response.status_code} adding history: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise SimklError(f"add_to_history request failed: {exc}") from exc
        return response.json()

    def add_to_watchlist(self, movies: list[dict], shows: list[dict]) -> dict:
        """POST a batch of items to ``/sync/add-to-list``.

        Used by the SeriesGuide migration script for items that came from
        a SeriesGuide custom list rather than watched history — Simkl has
        no custom-list API (confirmed absent from its OpenAPI spec, which
        explicitly notes "Custom user-created lists will get their own API
        in a future release"), so the closest equivalent is placing the
        item on one of Simkl's five built-in watchlist statuses via each
        item's own ``"to"`` key (e.g. ``"plantowatch"``).

        Per Simkl's own docs, don't call this for anything already sent to
        :meth:`add_to_history` in the same run — history writes already
        resolve the right status server-side, and a follow-up
        ``add-to-list`` call can downgrade that (e.g. revert a
        newly-completed show back to "plan to watch").

        Parameters:
            movies (list[dict]): Movie entries, each at minimum
                ``{"to": "plantowatch", "ids": {"tmdb": ...}}``.
            shows (list[dict]): Show entries, same shape.

        Returns:
            dict: Simkl's response, including ``added``/``not_found``.

        Raises:
            SimklError: On HTTP or request failure.
        """
        body: dict = {}
        if movies:
            body["movies"] = movies
        if shows:
            body["shows"] = shows
        try:
            response = self._http.post(
                "/sync/add-to-list",
                params=self._base_params(),
                json=body,
                headers=self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SimklError(
                f"HTTP {exc.response.status_code} adding to watchlist: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise SimklError(f"add_to_watchlist request failed: {exc}") from exc
        return response.json()

    def get_watched_movies(self) -> list[SimklMovie]:
        """Fetch every movie marked completed on Simkl, including anime movies.

        Simkl keeps anime (``anime_type`` of ``movie``, ``tv``, ``ova``,
        ``ona``, or ``special``) in its own library bucket, entirely
        separate from ``movies``/``shows`` — a Ghibli film watched on
        Simkl never appears under ``/sync/all-items/movies/...`` even
        though it's a single film, not an episodic show. So on top of the
        regular movies list, every anime entry with ``anime_type ==
        "movie"`` is folded in here (the rest go to
        :meth:`get_watched_episodes`).

        Returns:
            list[SimklMovie]: Watched movies with a TMDb ID. Entries
                without one are skipped since it's the identity key shared
                with jellyfin_popfeed.
        """
        raw: dict = self._get(
            "/sync/all-items/movies/completed", params={"extended": "full"}
        )
        movies = _parse_movie_entries(raw.get("movies", []), media_key="movie")

        anime_movie_entries = [
            entry for entry in self._get_watched_anime() if entry.get("anime_type") == "movie"
        ]
        movies += _parse_movie_entries(anime_movie_entries, media_key="show")

        logger.info("Fetched %d watched movies from Simkl (incl. anime)", len(movies))
        return movies

    def get_watched_episodes(self) -> list[SimklEpisode]:
        """Fetch every episode marked watched on Simkl, including anime shows.

        Uses status ``all`` rather than ``completed`` — on Simkl,
        ``completed`` means the *entire series* has been finished, so an
        in-progress show (status ``watching``/``plantowatch``/``hold``,
        which is most actively-watched TV) would otherwise be excluded
        entirely. ``include_all_episodes=yes`` is also required: Simkl
        only loads per-episode ``seasons[].episodes[]`` data by default
        for ``watching``/``plantowatch``/``hold`` items — ``completed``
        and ``dropped`` items return just a count unless this is set.

        Also folds in every anime entry that isn't ``anime_type ==
        "movie"`` (``tv``/``ova``/``ona``/``special``) — see
        :meth:`get_watched_movies` for why anime needs a separate fetch.

        Returns:
            list[SimklEpisode]: Watched episodes, identified by series
                TMDb ID + season + episode number. Shows without a TMDb ID
                are skipped entirely, as are individual episodes with no
                resolvable season/number (Simkl's TMDb-id coverage for
                episodes isn't fully guaranteed per community reports, but
                season/number are always present, which is all the
                jellyfin-compatible rkey scheme needs).
        """
        raw: dict = self._get(
            "/sync/all-items/shows/all",
            params={
                "extended": "full",
                "episode_watched_at": "yes",
                "include_all_episodes": "yes",
            },
        )
        episodes = _parse_episode_entries(raw.get("shows", []))

        anime_show_entries = [
            entry for entry in self._get_watched_anime() if entry.get("anime_type") != "movie"
        ]
        episodes += _parse_episode_entries(anime_show_entries)

        logger.info("Fetched %d watched episodes from Simkl (incl. anime)", len(episodes))
        return episodes

    def _get_watched_anime(self) -> list[dict]:
        """Fetch and cache every anime entry (any status, any anime_type).

        Shared by :meth:`get_watched_movies` and :meth:`get_watched_episodes`
        so a sync only issues one ``/sync/all-items/anime/...`` request
        instead of two, per Simkl's guidance not to over-poll ``all-items``.

        Returns:
            list[dict]: Raw entries from the ``anime`` key of the response.
        """
        if self._anime_cache is None:
            raw: dict = self._get(
                "/sync/all-items/anime/all",
                params={
                    "extended": "full",
                    "episode_watched_at": "yes",
                    "include_all_episodes": "yes",
                },
            )
            self._anime_cache = raw.get("anime", []) or []
        return self._anime_cache

    def get_movie_ratings(self) -> dict[int, int]:
        """Fetch the user's movie ratings, keyed by TMDb ID.

        Returns:
            dict[int, int]: Mapping of TMDb movie ID to rating (1-10).
                Returns an empty dict on any unexpected response shape
                rather than failing the whole sync — ratings are a
                secondary feature.
        """
        try:
            raw = self._get("/sync/ratings/movies")
        except SimklError as exc:
            logger.warning("Could not fetch movie ratings: %s", exc)
            return {}
        result: dict[int, int] = {}
        for entry in raw if isinstance(raw, list) else []:
            movie: dict = entry.get("movie") or {}
            tmdb_id = _parse_int((movie.get("ids") or {}).get("tmdb"))
            rating = entry.get("rating") or entry.get("user_rating")
            if tmdb_id and rating is not None:
                result[tmdb_id] = rating
        return result

    def get_episode_ratings(self) -> dict[tuple[int, int, int], int]:
        """Fetch the user's episode ratings, keyed by (show, season, ep).

        Returns:
            dict[tuple[int, int, int], int]: Mapping of
                ``(show_tmdb_id, season, number)`` to rating (1-10).
                Returns an empty dict on any unexpected response shape.
        """
        try:
            raw = self._get("/sync/ratings/episodes")
        except SimklError as exc:
            logger.warning("Could not fetch episode ratings: %s", exc)
            return {}
        result: dict[tuple[int, int, int], int] = {}
        for entry in raw if isinstance(raw, list) else []:
            show: dict = entry.get("show") or {}
            show_tmdb_id = _parse_int((show.get("ids") or {}).get("tmdb"))
            episode: dict = entry.get("episode") or {}
            rating = entry.get("rating") or entry.get("user_rating")
            season = episode.get("season")
            number = episode.get("number")
            if not show_tmdb_id or rating is None or season is None or number is None:
                continue
            result[(show_tmdb_id, season, number)] = rating
        return result


def movies_to_history_payload(movies: list[SimklMovie]) -> list[dict]:
    """Build ``POST /sync/history`` movie entries from SimklMovie objects.

    Used by ``sync.py`` to push Popfeed's existing watched-list items to
    Simkl. ``scripts/migrate_seriesguide.py`` has its own
    SeriesGuide-export-shaped equivalent since its source dicts use
    different field names entirely.

    Parameters:
        movies (list[SimklMovie]): Movies to mark watched on Simkl.

    Returns:
        list[dict]: Payload entries for the ``movies`` array.
    """
    payload: list[dict] = []
    for movie in movies:
        ids: dict = {"tmdb": movie.tmdb_id}
        if movie.imdb_id:
            ids["imdb"] = movie.imdb_id
        payload.append({"ids": ids, "status": "completed"})
    return payload


def episodes_to_history_payload(episodes: list[SimklEpisode]) -> list[dict]:
    """Build ``POST /sync/history`` show entries from SimklEpisode objects.

    Groups episodes by series (Simkl's history endpoint wants one entry
    per show with nested seasons/episodes, not one entry per episode).

    Parameters:
        episodes (list[SimklEpisode]): Episodes to mark watched on Simkl.

    Returns:
        list[dict]: Payload entries for the ``shows`` array.
    """
    shows: dict[int, dict] = {}
    for ep in episodes:
        show = shows.setdefault(
            ep.show_tmdb_id, {"ids": {"tmdb": ep.show_tmdb_id}, "seasons": {}}
        )
        if ep.show_imdb_id:
            show["ids"]["imdb"] = ep.show_imdb_id
        show["seasons"].setdefault(ep.season, []).append({"number": ep.number})

    payload: list[dict] = []
    for show in shows.values():
        payload.append(
            {
                "ids": show["ids"],
                "seasons": [
                    {"number": num, "episodes": eps}
                    for num, eps in sorted(show["seasons"].items())
                ],
            }
        )
    return payload


def send_in_batches(
    send_fn,
    key: str,
    items: list[dict],
    dry_run: bool = False,
    batch_size: int = 50,
    delay_seconds: float = 2.0,
) -> None:
    """POST ``items`` in batches via ``send_fn``, sequentially.

    Shared by the daily sync's Popfeed->Simkl push and
    ``scripts/migrate_seriesguide.py``. Simkl recommends batching ~50
    items per call rather than one call per item, and enforces a
    20-second per-user write lock that rejects concurrent writes — hence
    sequential sends with a delay between batches rather than one big
    request or parallel requests.

    Parameters:
        send_fn (Callable[..., dict]): ``client.add_to_history`` or
            ``client.add_to_watchlist`` — takes ``movies=`` / ``shows=``
            kwargs.
        key (str): ``"movies"`` or ``"shows"`` — which kwarg ``items``
            populates (the other is sent empty).
        items (list[dict]): Payload dicts to send.
        dry_run (bool): If True, log without sending.
        batch_size (int): Items per call.
        delay_seconds (float): Delay between batches.
    """
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        if dry_run:
            logger.info("[dry-run] Would send %d %s", len(batch), key)
            continue
        kwargs = (
            {"movies": batch, "shows": []}
            if key == "movies"
            else {"movies": [], "shows": batch}
        )
        result = send_fn(**kwargs)
        logger.info("Batch result: %s", result.get("added"))
        not_found = result.get("not_found", {})
        if any(not_found.values()):
            logger.warning("Some items not found by Simkl: %s", not_found)
        if i + batch_size < len(items):
            time.sleep(delay_seconds)


def _parse_int(value: Any) -> Optional[int]:
    """Best-effort int conversion for id fields Simkl sometimes returns as strings.

    Parameters:
        value (Any): Raw id value (int, numeric string, or None).

    Returns:
        Optional[int]: Parsed integer, or None if not parseable.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_movie_entries(entries: list[dict], media_key: str) -> list[SimklMovie]:
    """Turn ``all-items`` entries into SimklMovie objects.

    Parameters:
        entries (list[dict]): Raw entries from an ``all-items`` response
            (either the ``movies`` key, or ``anime`` filtered to
            ``anime_type == "movie"``).
        media_key (str): Key holding the item's metadata within each
            entry — ``"movie"`` for real movie entries, ``"show"`` for
            anime entries (Simkl keys anime movies under ``show`` too).

    Returns:
        list[SimklMovie]: Movies with a resolvable TMDb ID.
    """
    movies: list[SimklMovie] = []
    for entry in entries:
        media: dict = entry.get(media_key) or {}
        ids: dict = media.get("ids") or {}
        tmdb_id = _parse_int(ids.get("tmdb"))
        if not tmdb_id:
            logger.debug("Skipping movie without TMDb ID: %r", media.get("title"))
            continue
        movies.append(
            SimklMovie(
                tmdb_id=tmdb_id,
                imdb_id=ids.get("imdb"),
                title=media.get("title", ""),
                watched_at=entry.get("last_watched_at"),
            )
        )
    return movies


def _parse_episode_entries(entries: list[dict]) -> list[SimklEpisode]:
    """Turn ``all-items`` show entries into SimklEpisode objects.

    Parameters:
        entries (list[dict]): Raw entries from an ``all-items`` response
            (either the ``shows`` key, or ``anime`` filtered to
            non-``movie`` ``anime_type``s — both key their metadata under
            ``show``).

    Returns:
        list[SimklEpisode]: Episodes with a resolvable series TMDb ID and
            season/episode number.
    """
    episodes: list[SimklEpisode] = []
    for entry in entries:
        show: dict = entry.get("show") or {}
        ids: dict = show.get("ids") or {}
        show_tmdb_id = _parse_int(ids.get("tmdb"))
        if not show_tmdb_id:
            logger.debug("Skipping show without TMDb ID: %r", show.get("title"))
            continue
        for season in entry.get("seasons") or []:
            season_number = season.get("number")
            for ep in season.get("episodes") or []:
                number = ep.get("number")
                if season_number is None or number is None:
                    continue
                episodes.append(
                    SimklEpisode(
                        show_tmdb_id=show_tmdb_id,
                        show_imdb_id=ids.get("imdb"),
                        show_title=show.get("title", ""),
                        season=season_number,
                        number=number,
                        watched_at=ep.get("watched_at"),
                    )
                )
    return episodes


def wait_for_pin_approval(client: SimklClient, user_code: str, interval: int) -> str:
    """Poll until the PIN is approved, sleeping ``interval`` seconds between tries.

    Parameters:
        client (SimklClient): An unauthenticated SimklClient.
        user_code (str): The ``user_code`` from :meth:`SimklClient.request_pin`.
        interval (int): Seconds to wait between polls (from the same response).

    Returns:
        str: The access token, once approved.

    Raises:
        SimklError: If the PIN expires, is denied, or a request fails.
    """
    while True:
        try:
            return client.poll_pin(user_code)
        except SimklAuthPending:
            time.sleep(interval)
