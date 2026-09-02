"""Self-check for Config.load_profiles' multi-account env var scheme.

Run directly: python tests/test_config_profiles.py
"""

import os
from contextlib import contextmanager

from simkl_popfeed.config import Config, ConfigError

_NONEXISTENT_ENV_FILE = "/tmp/simkl-popfeed-test-nonexistent.env"


@contextmanager
def _env(values: dict[str, str]):
    """Set env vars for the duration of the block, then remove them."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_single_profile_bare_vars_still_works() -> None:
    with _env(
        {
            "SIMKL_CLIENT_ID": "cid",
            "SIMKL_ACCESS_TOKEN": "tok",
            "POPFEED_IDENTIFIER": "alice.bsky.social",
            "POPFEED_PASSWORD": "pw",
        }
    ):
        profiles = Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)

    assert len(profiles) == 1
    assert profiles[0].popfeed_identifier == "alice.bsky.social"
    assert profiles[0].simkl_client_id == "cid"


def test_multiple_profiles_share_client_id_and_tmdb_key() -> None:
    with _env(
        {
            "SIMKL_CLIENT_ID": "cid",
            "TMDB_API_KEY": "tmdb-key",
            "SIMKL_ACCESS_TOKEN": "tok1",
            "POPFEED_IDENTIFIER": "alice.bsky.social",
            "POPFEED_PASSWORD": "pw1",
            "SIMKL_ACCESS_TOKEN_2": "tok2",
            "POPFEED_IDENTIFIER_2": "bob.bsky.social",
            "POPFEED_PASSWORD_2": "pw2",
            "POPFEED_PDS_URL_2": "custom.pds.example",
        }
    ):
        profiles = Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)

    assert len(profiles) == 2
    assert [p.popfeed_identifier for p in profiles] == [
        "alice.bsky.social",
        "bob.bsky.social",
    ]
    assert all(p.simkl_client_id == "cid" for p in profiles)
    assert all(p.tmdb_api_key == "tmdb-key" for p in profiles)
    assert profiles[0].popfeed_pds_url == "https://eurosky.social"
    assert profiles[1].popfeed_pds_url == "https://custom.pds.example"
    assert profiles[0].simkl_access_token == "tok1"
    assert profiles[1].simkl_access_token == "tok2"


def test_explicit_suffix_1_works_for_profile_one() -> None:
    with _env(
        {
            "SIMKL_CLIENT_ID": "cid",
            "SIMKL_ACCESS_TOKEN_1": "tok",
            "POPFEED_IDENTIFIER_1": "alice.bsky.social",
            "POPFEED_PASSWORD_1": "pw",
        }
    ):
        profiles = Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)

    assert len(profiles) == 1
    assert profiles[0].popfeed_identifier == "alice.bsky.social"


def test_missing_client_id_raises() -> None:
    with _env(
        {
            "SIMKL_ACCESS_TOKEN": "tok",
            "POPFEED_IDENTIFIER": "alice.bsky.social",
            "POPFEED_PASSWORD": "pw",
        }
    ):
        os.environ.pop("SIMKL_CLIENT_ID", None)
        try:
            Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)
            raise AssertionError("expected ConfigError")
        except ConfigError as exc:
            assert "SIMKL_CLIENT_ID" in str(exc)


def test_no_profiles_configured_raises() -> None:
    with _env({"SIMKL_CLIENT_ID": "cid"}):
        for key in ("SIMKL_ACCESS_TOKEN", "POPFEED_IDENTIFIER", "POPFEED_PASSWORD"):
            os.environ.pop(key, None)
        try:
            Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)
            raise AssertionError("expected ConfigError")
        except ConfigError as exc:
            assert "POPFEED_IDENTIFIER" in str(exc)


def test_partially_configured_profile_raises_naming_the_gap() -> None:
    with _env(
        {
            "SIMKL_CLIENT_ID": "cid",
            "SIMKL_ACCESS_TOKEN": "tok",
            "POPFEED_IDENTIFIER": "alice.bsky.social",
            "POPFEED_PASSWORD": "pw",
            # Profile 2 started (identifier set) but missing the rest.
            "POPFEED_IDENTIFIER_2": "bob.bsky.social",
        }
    ):
        try:
            Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)
            raise AssertionError("expected ConfigError")
        except ConfigError as exc:
            assert "SIMKL_ACCESS_TOKEN_2" in str(exc)
            assert "POPFEED_PASSWORD_2" in str(exc)


def test_numbering_gap_raises_instead_of_silently_dropping() -> None:
    with _env(
        {
            "SIMKL_CLIENT_ID": "cid",
            "SIMKL_ACCESS_TOKEN": "tok",
            "POPFEED_IDENTIFIER": "alice.bsky.social",
            "POPFEED_PASSWORD": "pw",
            # Profile 2 skipped entirely; profile 3 set instead (likely typo).
            "SIMKL_ACCESS_TOKEN_3": "tok3",
            "POPFEED_IDENTIFIER_3": "carol.bsky.social",
            "POPFEED_PASSWORD_3": "pw3",
        }
    ):
        try:
            Config.load_profiles(env_file=_NONEXISTENT_ENV_FILE)
            raise AssertionError("expected ConfigError")
        except ConfigError as exc:
            assert "gap" in str(exc).lower()
            assert "_3" in str(exc)


if __name__ == "__main__":
    test_single_profile_bare_vars_still_works()
    test_multiple_profiles_share_client_id_and_tmdb_key()
    test_explicit_suffix_1_works_for_profile_one()
    test_missing_client_id_raises()
    test_no_profiles_configured_raises()
    test_partially_configured_profile_raises_naming_the_gap()
    test_numbering_gap_raises_instead_of_silently_dropping()
    print("ok")
