"""Self-check that already-tracked items are skipped, not overwritten.

Simulates jellyfin_popfeed having already written a watched-list record at
the shared deterministic rkey, then verifies simkl_popfeed's PopfeedClient
skips it entirely (no put_record calls at all) rather than updating it.

Run directly: python tests/test_dedup_gate.py
"""

from simkl_popfeed.models import SimklMovie
from simkl_popfeed.popfeed import PopfeedClient


class _FakeSession:
    did = "did:plc:fake"


class _FakeAtProtoClient:
    """Minimal stand-in for AtProtoClient, tracking calls made against it."""

    def __init__(self, existing_rkeys: set[str]) -> None:
        self._existing = existing_rkeys
        self.put_calls: list[tuple[str, str]] = []
        self.session = _FakeSession()

    def get_record(self, did: str, collection: str, rkey: str):
        return {"value": {}} if rkey in self._existing else None

    def put_record(self, did: str, collection: str, rkey: str, record: dict):
        self.put_calls.append((collection, rkey))
        return {"uri": f"at://{did}/{collection}/{rkey}"}


def test_skips_when_watched_rkey_already_exists() -> None:
    atproto = _FakeAtProtoClient(existing_rkeys={"w.mv.603"})
    popfeed = PopfeedClient(atproto)
    movie = SimklMovie(tmdb_id=603, title="The Matrix")

    written = popfeed.sync_movie(movie, list_uris={"watched_movies": "at://x"})

    assert written is False
    assert atproto.put_calls == []


def test_writes_when_watched_rkey_is_absent() -> None:
    atproto = _FakeAtProtoClient(existing_rkeys=set())
    popfeed = PopfeedClient(atproto)
    movie = SimklMovie(tmdb_id=603, title="The Matrix", watched_at="2026-01-01T00:00:00Z")

    written = popfeed.sync_movie(
        movie, list_uris={"watched_movies": "at://x", "recent": "at://y"}
    )

    assert written is True
    written_rkeys = {rkey for _, rkey in atproto.put_calls}
    assert "w.mv.603" in written_rkeys
    assert "r.mv.603" in written_rkeys
    # No rating on this movie, so no review record should be written.
    assert "rv.mv.603" not in written_rkeys


if __name__ == "__main__":
    test_skips_when_watched_rkey_already_exists()
    test_writes_when_watched_rkey_is_absent()
    print("ok")
