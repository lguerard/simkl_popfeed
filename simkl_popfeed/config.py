"""Configuration loading from environment variables."""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


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
    def from_env(cls, env_file: str = ".env", dry_run: bool = False) -> "Config":
        """Load configuration from environment, optionally from a file.

        Parameters:
            env_file (str): Path to a .env file to load.
            dry_run (bool): Override for the dry-run flag.

        Returns:
            Config: Populated configuration instance.

        Raises:
            ConfigError: If a required variable is missing.
        """
        load_dotenv(dotenv_path=env_file)

        required = {
            "SIMKL_CLIENT_ID": "simkl_client_id",
            "SIMKL_ACCESS_TOKEN": "simkl_access_token",
            "POPFEED_IDENTIFIER": "popfeed_identifier",
            "POPFEED_PASSWORD": "popfeed_password",
        }
        values: dict[str, str] = {}
        missing: list[str] = []
        for env_var in required:
            value = os.environ.get(env_var, "").strip()
            if not value:
                missing.append(env_var)
            values[env_var] = value
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        env_dry_run = os.environ.get("DRY_RUN", "").strip().lower()
        resolved_dry_run = dry_run or env_dry_run in ("1", "true", "yes")

        raw_pds_url = (
            os.environ.get("POPFEED_PDS_URL") or "https://eurosky.social"
        ).strip()

        list_names: dict[str, str] = {}
        overrides = {
            "watched_movies": "SIMKL_POPFEED_WATCHED_MOVIES_LIST_NAME",
            "watched_tv_shows": "SIMKL_POPFEED_WATCHED_SHOWS_LIST_NAME",
            "recent": "SIMKL_POPFEED_RECENT_LIST_NAME",
        }
        for list_type, env_var in overrides.items():
            override = os.environ.get(env_var, "").strip()
            if override:
                list_names[list_type] = override

        return cls(
            simkl_client_id=values["SIMKL_CLIENT_ID"],
            simkl_access_token=values["SIMKL_ACCESS_TOKEN"],
            popfeed_identifier=values["POPFEED_IDENTIFIER"],
            popfeed_password=values["POPFEED_PASSWORD"],
            popfeed_pds_url=_ensure_https(raw_pds_url),
            list_names=list_names,
            dry_run=resolved_dry_run,
            tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip() or None,
        )
