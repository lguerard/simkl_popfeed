"""Self-check that rkeys match jellyfin_popfeed's PopfeedRkeyBuilder scheme.

Run directly: python tests/test_rkey_builders.py
"""

from simkl_popfeed.popfeed import (
    _episode_id_key,
    _movie_id_key,
    _recent_rkey,
    _review_rkey,
    _watched_rkey,
)


def test_movie_rkeys_match_jellyfin_scheme() -> None:
    id_key = _movie_id_key(603)
    assert id_key == "mv.603"
    assert _watched_rkey(id_key) == "w.mv.603"
    assert _recent_rkey(id_key) == "r.mv.603"
    assert _review_rkey(id_key) == "rv.mv.603"


def test_episode_rkeys_match_jellyfin_scheme() -> None:
    id_key = _episode_id_key(1399, 1, 1)
    assert id_key == "ep.1399.1.1"
    assert _watched_rkey(id_key) == "w.ep.1399.1.1"
    assert _recent_rkey(id_key) == "r.ep.1399.1.1"
    assert _review_rkey(id_key) == "rv.ep.1399.1.1"


def test_different_items_get_different_keys() -> None:
    assert _movie_id_key(603) != _movie_id_key(604)
    assert _episode_id_key(1399, 1, 1) != _episode_id_key(1399, 1, 2)
    assert _episode_id_key(1399, 1, 1) != _episode_id_key(1400, 1, 1)


if __name__ == "__main__":
    test_movie_rkeys_match_jellyfin_scheme()
    test_episode_rkeys_match_jellyfin_scheme()
    test_different_items_get_different_keys()
    print("ok")
