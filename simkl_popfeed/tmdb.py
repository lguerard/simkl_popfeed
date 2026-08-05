"""TMDb API client.

Used only to look up how many episodes each season of a TV show actually
has. Neither Simkl's sync data nor Popfeed's watched-list records expose
season/series *totals* — they only ever list what's been watched — so
there's no way to tell "season complete" from "still missing episodes"
without an external source of truth. jellyfin_popfeed gets this for free
from Jellyfin's own media library; simkl_popfeed has no library, so it
asks TMDb instead (the same ID scheme already canonical everywhere else
in this project).
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TMDB_API_URL = "https://api.themoviedb.org/3"


class TmdbError(Exception):
    """Raised when the TMDb API returns an error."""


class TmdbClient:
    """Client for the parts of the TMDb API this project needs.

    Parameters:
        api_key (str): TMDb API key (v3 auth — a free account at
            themoviedb.org/settings/api is enough, no approval process).
    """

    def __init__(self, api_key: str) -> None:
        """Initialise the client.

        Parameters:
            api_key (str): TMDb API key.
        """
        self._http = httpx.Client(
            base_url=TMDB_API_URL, params={"api_key": api_key}, timeout=30.0
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "TmdbClient":
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close the HTTP client on context exit."""
        self.close()

    def get_season_episode_counts(self, series_tmdb_id: int) -> dict[int, int]:
        """Return each season's total episode count for a TV series.

        Season 0 (TMDb's specials bucket) is excluded, matching
        jellyfin_popfeed's own completion accounting — specials never
        gate a season or series being marked complete.

        Parameters:
            series_tmdb_id (int): TMDb TV series ID.

        Returns:
            dict[int, int]: Mapping of season number to episode count.

        Raises:
            TmdbError: On HTTP or request failure.
        """
        try:
            response = self._http.get(f"/tv/{series_tmdb_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TmdbError(
                f"HTTP {exc.response.status_code} from TMDb API for series {series_tmdb_id}"
            ) from exc
        except httpx.RequestError as exc:
            raise TmdbError(f"Request to TMDb API failed: {exc}") from exc

        data: dict = response.json()
        return {
            season["season_number"]: season["episode_count"]
            for season in data.get("seasons", [])
            if season.get("season_number", 0) > 0 and season.get("episode_count")
        }
