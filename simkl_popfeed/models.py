"""Data models for Simkl and Popfeed entities."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SimklMovie:
    """A watched movie entry from a Simkl profile.

    Parameters:
        tmdb_id (int): TMDb movie ID (canonical Popfeed identity).
        imdb_id (Optional[str]): IMDb ID, stored as supplementary metadata.
        title (str): Movie title.
        watched_at (Optional[str]): ISO-8601 timestamp of the last watch.
        rating (Optional[int]): User rating on Simkl's 1-10 scale.
    """

    tmdb_id: int
    imdb_id: Optional[str] = None
    title: str = ""
    watched_at: Optional[str] = None
    rating: Optional[int] = None


@dataclass
class SimklEpisode:
    """A watched episode entry from a Simkl profile.

    Identity follows Jellyfin Popfeed's convention: the *series* TMDb ID
    plus season/episode number, not a per-episode TMDb ID (Simkl's watched
    history doesn't expose one for episodes).

    Parameters:
        show_tmdb_id (int): TMDb series ID (canonical Popfeed identity).
        show_imdb_id (Optional[str]): IMDb series ID, supplementary.
        show_title (str): Series title.
        season (int): Season number.
        number (int): Episode number within the season.
        watched_at (Optional[str]): ISO-8601 timestamp of the last watch.
        rating (Optional[int]): User rating on Simkl's 1-10 scale.
    """

    show_tmdb_id: int
    show_imdb_id: Optional[str] = None
    show_title: str = ""
    season: int = 0
    number: int = 0
    watched_at: Optional[str] = None
    rating: Optional[int] = None


@dataclass
class PopfeedIdentifiers:
    """Identifiers used to match an item on Popfeed.

    Mirrors jellyfin_popfeed's ``PopfeedIdentifiers`` shape so records from
    either source describe the same item identically.

    Parameters:
        imdb_id (Optional[str]): IMDb ID.
        tmdb_id (Optional[int]): TMDb movie ID (movies only).
        tmdb_tv_series_id (Optional[int]): TMDb series ID (episodes only).
        season_number (Optional[int]): Season number (episodes only).
        episode_number (Optional[int]): Episode number (episodes only).
    """

    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    tmdb_tv_series_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None

    def as_dict(self) -> dict:
        """Return a dict containing only non-None identifier fields.

        Returns:
            dict: Mapping of identifier key to value.
        """
        result: dict = {}
        if self.imdb_id:
            result["imdbId"] = self.imdb_id
        if self.tmdb_id is not None:
            result["tmdbId"] = self.tmdb_id
        if self.tmdb_tv_series_id is not None:
            result["tmdbTvSeriesId"] = self.tmdb_tv_series_id
        if self.season_number is not None:
            result["seasonNumber"] = self.season_number
        if self.episode_number is not None:
            result["episodeNumber"] = self.episode_number
        return result
