"""Self-check for SimklClient.get_watched_movies/get_watched_episodes.

Covers two real bugs fixed in this file:
1. Episodes only came from shows Simkl considers fully "completed",
   silently dropping every in-progress show (most actively-watched TV).
2. Anime — including anime movies like a Ghibli film — lives in its own
   Simkl library bucket entirely separate from movies/shows, so it never
   synced at all regardless of #1.

Uses httpx.MockTransport (part of httpx itself), same approach as
tests/test_tmdb_client.py.

Run directly: python tests/test_simkl_watched_fetch.py
"""

import httpx

from simkl_popfeed.simkl import SimklClient


def _fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/all-items/movies/completed":
            return httpx.Response(
                200,
                json={
                    "movies": [
                        {
                            "last_watched_at": "2026-01-01T00:00:00Z",
                            "movie": {
                                "title": "The Matrix",
                                "ids": {"tmdb": 603, "imdb": "tt0133093"},
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/sync/all-items/shows/all":
            return httpx.Response(
                200,
                json={
                    "shows": [
                        # In-progress show — previously excluded entirely
                        # by the old status=completed filter.
                        {
                            "status": "watching",
                            "show": {
                                "title": "Ongoing Show",
                                "ids": {"tmdb": 1000, "imdb": "tt1000000"},
                            },
                            "seasons": [
                                {
                                    "number": 1,
                                    "episodes": [
                                        {"number": 1, "watched_at": "2026-01-01T00:00:00Z"},
                                        {"number": 2, "watched_at": "2026-01-02T00:00:00Z"},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/sync/all-items/anime/all":
            return httpx.Response(
                200,
                json={
                    "anime": [
                        # Anime movie (e.g. a Ghibli film) — must land
                        # in get_watched_movies(), not get_watched_episodes().
                        {
                            "anime_type": "movie",
                            "last_watched_at": "2026-01-03T00:00:00Z",
                            "show": {
                                "title": "Spirited Away",
                                "ids": {"tmdb": 129, "imdb": "tt0245429"},
                            },
                            "seasons": [
                                {"number": 1, "episodes": [{"number": 1}]}
                            ],
                        },
                        # Anime TV series — must land in get_watched_episodes().
                        {
                            "anime_type": "tv",
                            "show": {
                                "title": "Cowboy Bebop",
                                "ids": {"tmdb": 30991, "imdb": "tt0213338"},
                            },
                            "seasons": [
                                {
                                    "number": 1,
                                    "episodes": [
                                        {"number": 1, "watched_at": "2026-01-04T00:00:00Z"}
                                    ],
                                }
                            ],
                        },
                        # No TMDb ID — must be skipped, not crash.
                        {
                            "anime_type": "movie",
                            "show": {"title": "No TMDb Match", "ids": {"mal": "999"}},
                        },
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url.path}")

    return httpx.MockTransport(handler)


def _make_client() -> SimklClient:
    client = SimklClient(client_id="cid", access_token="token")
    client._http = httpx.Client(
        base_url="https://api.simkl.com", transport=_fake_transport()
    )
    return client


def test_get_watched_movies_includes_anime_movies() -> None:
    client = _make_client()

    movies = client.get_watched_movies()

    tmdb_ids = {m.tmdb_id for m in movies}
    assert tmdb_ids == {603, 129}
    spirited_away = next(m for m in movies if m.tmdb_id == 129)
    assert spirited_away.title == "Spirited Away"


def test_get_watched_episodes_includes_in_progress_and_anime_shows() -> None:
    client = _make_client()

    episodes = client.get_watched_episodes()

    show_ids = {ep.show_tmdb_id for ep in episodes}
    # 1000 = in-progress ("watching") show, previously dropped entirely.
    # 30991 = anime TV series, previously never fetched at all.
    assert show_ids == {1000, 30991}
    assert sum(1 for ep in episodes if ep.show_tmdb_id == 1000) == 2


def test_anime_fetch_is_cached_across_both_calls() -> None:
    client = _make_client()
    calls: list[str] = []
    original_get = client._get

    def counting_get(path: str, params=None):
        calls.append(path)
        return original_get(path, params)

    client._get = counting_get  # type: ignore[method-assign]

    client.get_watched_movies()
    client.get_watched_episodes()

    assert calls.count("/sync/all-items/anime/all") == 1


if __name__ == "__main__":
    test_get_watched_movies_includes_anime_movies()
    test_get_watched_episodes_includes_in_progress_and_anime_shows()
    test_anime_fetch_is_cached_across_both_calls()
    print("ok")
