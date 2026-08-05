"""Self-check for season/series completion marking and the status-value fix.

Covers two things: that watched-list/recent records use Popfeed's actual
accepted status literal ("#finished", not "finished" — the bug that made
synced items never show as watched), and that PopfeedClient.sync_show_progress
marks a season complete only once every one of its episodes is watched,
and a series complete only once every one of its seasons is.

Run directly: python tests/test_show_progress.py
"""

from simkl_popfeed.models import SimklEpisode, SimklMovie
from simkl_popfeed.popfeed import PopfeedClient


class _FakeSession:
    did = "did:plc:fake"


class _FakeAtProtoClient:
    """Stand-in for AtProtoClient that records full call detail."""

    def __init__(self, existing_rkeys: set[str] | None = None) -> None:
        self._existing = existing_rkeys or set()
        self.put_calls: list[tuple[str, str, dict]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.session = _FakeSession()

    def get_record(self, did: str, collection: str, rkey: str):
        return {"value": {}} if rkey in self._existing else None

    def put_record(self, did: str, collection: str, rkey: str, record: dict):
        self.put_calls.append((collection, rkey, record))
        return {"uri": f"at://{did}/{collection}/{rkey}"}

    def delete_record(self, did: str, collection: str, rkey: str):
        self.delete_calls.append((collection, rkey))

    def record_for(self, rkey: str) -> dict:
        for _, r, record in self.put_calls:
            if r == rkey:
                return record
        raise KeyError(rkey)


_LIST_URIS = {"watched_movies": "at://x/movies", "watched_tv_shows": "at://x/shows"}


def test_movie_watched_record_uses_hash_finished_status() -> None:
    atproto = _FakeAtProtoClient()
    popfeed = PopfeedClient(atproto)
    movie = SimklMovie(tmdb_id=603, title="The Matrix")

    popfeed.sync_movie(movie, list_uris={**_LIST_URIS, "recent": "at://x/recent"})

    watched = atproto.record_for("w.mv.603")
    recent = atproto.record_for("r.mv.603")
    assert watched["status"] == "#finished"
    assert recent["status"] == "#finished"
    assert recent["listType"] == "recent"


def test_season_marked_complete_only_when_every_episode_watched() -> None:
    atproto = _FakeAtProtoClient()
    popfeed = PopfeedClient(atproto)
    episodes = [
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=1),
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=2),
    ]
    # Season 1 has 3 episodes total, only 2 watched -> not complete.
    popfeed.sync_show_progress(episodes, {1399: {1: 3}}, _LIST_URIS)

    assert atproto.delete_calls == [("social.popfeed.feed.listItem", "w.ts.1399.1")]
    show_record = atproto.record_for("w.tv.1399")
    assert show_record["status"] == "#in_progress"
    assert "completedAt" not in show_record
    assert show_record["watchedEpisodes"] == [
        {"seasonNumber": 1, "episodeNumber": 1},
        {"seasonNumber": 1, "episodeNumber": 2},
    ]


def test_season_and_series_marked_complete_when_all_episodes_watched() -> None:
    atproto = _FakeAtProtoClient()
    popfeed = PopfeedClient(atproto)
    episodes = [
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=1),
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=2),
    ]
    # Season 1 has exactly 2 episodes, both watched, and it's the only season -> complete.
    popfeed.sync_show_progress(episodes, {1399: {1: 2}}, _LIST_URIS)

    season_record = atproto.record_for("w.ts.1399.1")
    assert season_record["status"] == "#finished"
    assert season_record["identifiers"] == {"tmdbTvSeriesId": 1399, "seasonNumber": 1}
    assert season_record["title"] == "GoT - Season 01"

    show_record = atproto.record_for("w.tv.1399")
    assert show_record["status"] == "#finished"
    assert show_record["completedAt"]
    # tv_show records use tmdbId, not tmdbTvSeriesId -- matches jellyfin_popfeed exactly.
    assert show_record["identifiers"] == {"tmdbId": 1399}


def test_series_stays_in_progress_if_any_season_incomplete() -> None:
    atproto = _FakeAtProtoClient()
    popfeed = PopfeedClient(atproto)
    episodes = [
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=1, number=1),
        SimklEpisode(show_tmdb_id=1399, show_title="GoT", season=2, number=1),
    ]
    # Season 1 complete (1/1), season 2 incomplete (1/5) -> series not complete.
    popfeed.sync_show_progress(episodes, {1399: {1: 1, 2: 5}}, _LIST_URIS)

    assert atproto.record_for("w.ts.1399.1")["status"] == "#finished"
    assert ("social.popfeed.feed.listItem", "w.ts.1399.2") in atproto.delete_calls
    assert atproto.record_for("w.tv.1399")["status"] == "#in_progress"


def test_show_without_tmdb_totals_is_skipped() -> None:
    atproto = _FakeAtProtoClient()
    popfeed = PopfeedClient(atproto)
    episodes = [SimklEpisode(show_tmdb_id=9999, show_title="Unknown", season=1, number=1)]

    popfeed.sync_show_progress(episodes, season_totals={}, list_uris=_LIST_URIS)

    assert atproto.put_calls == []
    assert atproto.delete_calls == []


if __name__ == "__main__":
    test_movie_watched_record_uses_hash_finished_status()
    test_season_marked_complete_only_when_every_episode_watched()
    test_season_and_series_marked_complete_when_all_episodes_watched()
    test_series_stays_in_progress_if_any_season_incomplete()
    test_show_without_tmdb_totals_is_skipped()
    print("ok")
