"""Self-check for TmdbClient's season/episode count parsing.

Uses httpx.MockTransport (part of httpx itself), same approach as
tests/test_jellyfin_client.py used before the Jellyfin path was removed.

Run directly: python tests/test_tmdb_client.py
"""

import httpx

from simkl_popfeed.tmdb import TmdbClient


def _fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "seasons": [
                    {"season_number": 0, "episode_count": 4},  # specials -- excluded
                    {"season_number": 1, "episode_count": 10},
                    {"season_number": 2, "episode_count": 8},
                    {"season_number": 3, "episode_count": 0},  # unaired -- excluded
                ]
            },
        )

    return httpx.MockTransport(handler)


def test_get_season_episode_counts_excludes_specials_and_zero_counts() -> None:
    client = TmdbClient(api_key="k")
    client._http = httpx.Client(
        base_url="https://api.themoviedb.org/3", transport=_fake_transport()
    )

    counts = client.get_season_episode_counts(1399)

    assert counts == {1: 10, 2: 8}


if __name__ == "__main__":
    test_get_season_episode_counts_excludes_specials_and_zero_counts()
    print("ok")
