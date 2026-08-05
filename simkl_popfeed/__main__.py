"""CLI entry point for simkl-popfeed."""

import argparse
import os
import sys

from dotenv import load_dotenv

from simkl_popfeed.config import Config, ConfigError
from simkl_popfeed.simkl import SimklClient, wait_for_pin_approval
from simkl_popfeed.sync import run_sync


def _run_setup(env_file: str) -> None:
    """Run the one-time PIN/device-code flow and print the access token.

    Requires only ``SIMKL_CLIENT_ID`` to be set (in the environment or the
    env file) — the whole point of this mode is to obtain
    ``SIMKL_ACCESS_TOKEN``, so it can't require that variable itself.

    Parameters:
        env_file (str): Path to a .env file to load ``SIMKL_CLIENT_ID`` from.
    """
    load_dotenv(dotenv_path=env_file)
    client_id = os.environ.get("SIMKL_CLIENT_ID", "").strip()
    if not client_id:
        print("Configuration error: SIMKL_CLIENT_ID is not set", file=sys.stderr)
        sys.exit(1)

    client = SimklClient(client_id)
    pin = client.request_pin()
    print(f"Go to {pin['verification_url']} and enter code: {pin['user_code']}")
    print("Waiting for approval...")
    token = wait_for_pin_approval(client, pin["user_code"], pin.get("interval", 5))
    print("\nSIMKL_ACCESS_TOKEN=" + token)
    print("\nSave this as SIMKL_ACCESS_TOKEN in your .env file and as a GitHub")
    print("Actions secret. This token is long-lived (~5 years) — no need to")
    print("repeat this setup unless it's revoked.")


def main() -> None:
    """Parse arguments and run the sync (or the one-time setup flow)."""
    parser = argparse.ArgumentParser(
        prog="simkl-popfeed",
        description=(
            "Sync a Simkl profile's watch history to Popfeed via AT "
            "Protocol, skipping anything already tracked by "
            "jellyfin_popfeed."
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        default=False,
        help="Run the one-time Simkl PIN auth flow to obtain SIMKL_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log actions without writing anything to Popfeed.",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        default=".env",
        help="Path to .env file (default: .env).",
    )
    args = parser.parse_args()

    if args.setup:
        _run_setup(args.env_file)
        return

    try:
        config = Config.from_env(env_file=args.env_file, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    run_sync(config)


if __name__ == "__main__":
    main()
