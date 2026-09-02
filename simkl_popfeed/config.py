"""Configuration loading from environment variables."""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# Env vars every profile needs; each is read unsuffixed (or "_1") for
# profile 1 and "_N" for later profiles. Presence of any one of these for
# a given index is what marks that index as "a profile was started here".
_PROFILE_REQUIRED = ("SIMKL_ACCESS_TOKEN", "POPFEED_IDENTIFIER", "POPFEED_PASSWORD")

# ponytail: sane upper bound so a stray suffixed env var can't make the
# profile scan loop indefinitely. Raise if a real setup ever needs more.
_MAX_PROFILES = 25


def _profile_env(base_name: str, index: int) -> str:
    """Read an env var for a given profile index.

    Profile 1 checks ``{base_name}_1`` first, falling back to the bare
    ``{base_name}`` (so existing single-profile ``.env`` files keep
    working unchanged). Every later profile only checks ``{base_name}_{index}``.

    Parameters:
        base_name (str): Unsuffixed env var name, e.g. ``"POPFEED_IDENTIFIER"``.
        index (int): Profile number (1-based).

    Returns:
        str: The resolved value, stripped, or ``""`` if unset.
    """
    value = os.environ.get(f"{base_name}_{index}", "").strip()
    if value or index != 1:
        return value
    return os.environ.get(base_name, "").strip()


def _ensure_https(url: str) -> str:
    """Ensure a URL has an https:// scheme.

    Parameters:
        url (str): Raw URL string, possibly without a scheme.

    Returns:
        str: URL with ``https://`` scheme.
    """
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return "https://" + url


@dataclass
class Config:
    """Application configuration loaded from environment variables.

    Parameters:
        simkl_client_id (str): Simkl API application client ID.
        simkl_access_token (str): User access token from the PIN flow.
        popfeed_identifier (str): Popfeed/Bluesky handle or DID.
        popfeed_password (str): Popfeed/Bluesky app password.
        popfeed_pds_url (str): AT Protocol PDS base URL.
        list_names (dict[str, str]): Overrides for Watched Movies/Shows/
            Recent list display names, keyed by list type.
        dry_run (bool): If True, log actions without writing to Popfeed.
        tmdb_api_key (Optional[str]): TMDb API key. When set, seasons and
            series get marked complete on Popfeed once every episode is
            watched (needs TMDb for total episode counts, since neither
            Simkl nor Popfeed expose those). When unset, this step is
            skipped entirely — everything else still works.
    """

    simkl_client_id: str
    simkl_access_token: str
    popfeed_identifier: str
    popfeed_password: str
    popfeed_pds_url: str = "https://eurosky.social"
    list_names: dict = field(default_factory=dict)
    dry_run: bool = False
    tmdb_api_key: Optional[str] = None

    @classmethod
    def load_profiles(cls, env_file: str = ".env", dry_run: bool = False) -> list["Config"]:
        """Load one Config per configured Popfeed/Simkl account pairing.

        Supports multiple people syncing through one run (e.g. everyone on
        a shared Jellyfin server, each with their own Simkl + Popfeed
        account): profile 1's variables are unsuffixed (``SIMKL_ACCESS_TOKEN``,
        ``POPFEED_IDENTIFIER``, ``POPFEED_PASSWORD``, ``POPFEED_PDS_URL``,
        the ``SIMKL_POPFEED_*_LIST_NAME`` overrides) or may use an explicit
        ``_1`` suffix; every profile after that needs the ``_2``, ``_3``, ...
        suffix. ``SIMKL_CLIENT_ID`` and ``TMDB_API_KEY`` are shared across
        every profile — one Simkl app registration and one TMDb key cover
        any number of user tokens.

        Scanning stops at the first index with none of
        ``SIMKL_ACCESS_TOKEN``/``POPFEED_IDENTIFIER``/``POPFEED_PASSWORD``
        set for it.

        Parameters:
            env_file (str): Path to a .env file to load.
            dry_run (bool): Override for the dry-run flag, applied to every
                profile.

        Returns:
            list[Config]: One Config per discovered profile, in order.

        Raises:
            ConfigError: If ``SIMKL_CLIENT_ID`` is missing, no profile is
                configured at all, or a profile that was started (has at
                least one of its three required variables set) is missing
                the others.
        """
        load_dotenv(dotenv_path=env_file)

        client_id = os.environ.get("SIMKL_CLIENT_ID", "").strip()
        if not client_id:
            raise ConfigError("Missing required environment variables: SIMKL_CLIENT_ID")
        tmdb_api_key = os.environ.get("TMDB_API_KEY", "").strip() or None

        env_dry_run = os.environ.get("DRY_RUN", "").strip().lower()
        resolved_dry_run = dry_run or env_dry_run in ("1", "true", "yes")

        profiles: list[Config] = []
        index = 1
        while index <= _MAX_PROFILES:
            if not any(_profile_env(name, index) for name in _PROFILE_REQUIRED):
                break
            profiles.append(
                cls._load_profile(
                    index, client_id, tmdb_api_key, resolved_dry_run
                )
            )
            index += 1

        if not profiles:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(_PROFILE_REQUIRED)
            )

        # A gap in the numbering (e.g. "_3" set but "_2" missing) would
        # otherwise silently drop that profile, since the scan above just
        # stops at the first unconfigured index — catch it instead.
        orphaned = [
            f"{name}_{n}"
            for n in range(index, _MAX_PROFILES + 1)
            for name in _PROFILE_REQUIRED
            if _profile_env(name, n)
        ]
        if orphaned:
            raise ConfigError(
                f"Profile {index} is not configured, but later-numbered "
                "profile variables are set (check for a gap or typo in the "
                "numbering): " + ", ".join(orphaned)
            )

        return profiles

    @classmethod
    def _load_profile(
        cls, index: int, client_id: str, tmdb_api_key: Optional[str], dry_run: bool
    ) -> "Config":
        """Build one profile's Config, or raise if it's only partially set.

        Parameters:
            index (int): Profile number (1-based).
            client_id (str): Shared Simkl client ID.
            tmdb_api_key (Optional[str]): Shared TMDb API key.
            dry_run (bool): Resolved dry-run flag to apply.

        Returns:
            Config: The profile's configuration.

        Raises:
            ConfigError: If any of the three required variables for this
                profile is missing.
        """
        missing = [name for name in _PROFILE_REQUIRED if not _profile_env(name, index)]
        if missing:
            suffix = "" if index == 1 else f"_{index}"
            raise ConfigError(
                f"Profile {index} is missing required environment variables: "
                + ", ".join(f"{name}{suffix}" for name in missing)
            )

        raw_pds_url = _profile_env("POPFEED_PDS_URL", index) or "https://eurosky.social"

        list_names: dict[str, str] = {}
        overrides = {
            "watched_movies": "SIMKL_POPFEED_WATCHED_MOVIES_LIST_NAME",
            "watched_tv_shows": "SIMKL_POPFEED_WATCHED_SHOWS_LIST_NAME",
            "recent": "SIMKL_POPFEED_RECENT_LIST_NAME",
        }
        for list_type, base_name in overrides.items():
            override = _profile_env(base_name, index)
            if override:
                list_names[list_type] = override

        return cls(
            simkl_client_id=client_id,
            simkl_access_token=_profile_env("SIMKL_ACCESS_TOKEN", index),
            popfeed_identifier=_profile_env("POPFEED_IDENTIFIER", index),
            popfeed_password=_profile_env("POPFEED_PASSWORD", index),
            popfeed_pds_url=_ensure_https(raw_pds_url),
            list_names=list_names,
            dry_run=dry_run,
            tmdb_api_key=tmdb_api_key,
        )
