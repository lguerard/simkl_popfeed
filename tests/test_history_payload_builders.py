"""Self-check for converting watched items to Simkl /sync/history payloads.

Source-agnostic: used both for pushing Popfeed's existing watched-list
items to Simkl (sync.py) and by scripts/migrate_seriesguide.py.

Run directly: python tests/test_history_payload_builders.py
"""

from simkl_popfeed.models import SimklEpisode, SimklMovie
from simkl_popfeed.simkl import episodes_to_history_payload, movies_to_history_payload


def test_movies_to_history_payload_includes_imdb_when_present() -> None:
    movies = [
        SimklMovie(tmdb_id=603, imdb_id="tt0133093", title="The Matrix"),
        SimklMovie(tmdb_id=604, title="No IMDb id"),
    ]
    payload = movies_to_history_payload(movies)
    assert payload == [
        {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "status": "completed"},
        {"ids": {"tmdb": 604}, "status": "completed"},
    ]


def test_episodes_to_history_payload_groups_by_show_and_season() -> None:
    episodes = [
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=1),
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=2),
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=2, number=1),
        SimklEpisode(
            show_tmdb_id=1400,
            show_imdb_id="tt9999999",
            show_title="Other Show",
            season=1,
            number=1,
        ),
    ]
    payload = episodes_to_history_payload(episodes)
    by_tmdb = {p["ids"]["tmdb"]: p for p in payload}

    assert by_tmdb[1399]["seasons"] == [
        {"number": 1, "episodes": [{"number": 1}, {"number": 2}]},
        {"number": 2, "episodes": [{"number": 1}]},
    ]
    assert by_tmdb[1400]["ids"] == {"tmdb": 1400, "imdb": "tt9999999"}


if __name__ == "__main__":
    test_movies_to_history_payload_includes_imdb_when_present()
    test_episodes_to_history_payload_groups_by_show_and_season()
    print("ok")
