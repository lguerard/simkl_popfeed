#!/usr/bin/env python3
"""One-time fix: correct legacy status literals on existing Popfeed records.

Before the fix in commit b015d97, simkl_popfeed wrote ``status: "finished"``
instead of Popfeed's actual accepted value ``"#finished"`` (and would have
written ``"in_progress"`` instead of ``"#in_progress"`` for tv_show
progress records, though those didn't exist before this same fix landed).
ATProto doesn't validate token values against the lexicon on write, so
those records were created successfully but never registered as watched
in the Popfeed app. New syncs write the correct value from now on, but
existing bad records are never revisited by the regular sync (its dedup
check only looks at whether a record exists, not whether its content is
correct) -- this script is the one-time backfill to correct them.

Scans every ``social.popfeed.feed.listItem`` record under the account and
rewrites any with a legacy bare-word status, preserving everything else
about the record untouched.

Usage:
    python scripts/fix_legacy_status.py [--dry-run]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from simkl_popfeed.atproto import AtProtoClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

_COLLECTION_LIST_ITEM = "social.popfeed.feed.listItem"

# Legacy bare-word value -> Popfeed's actual accepted literal.
_LEGACY_STATUS_FIXES = {
    "finished": "#finished",
    "in_progress": "#in_progress",
}


def corrected_status(status: str) -> str | None:
    """Return the fixed status value for a legacy literal, or None if fine.

    Parameters:
        status (str): The record's current ``status`` field value.

    Returns:
        str | None: The corrected value if ``status`` is a known legacy
            literal, otherwise None (nothing to fix).
    """
    return _LEGACY_STATUS_FIXES.get(status)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file)
    identifier = os.environ.get("POPFEED_IDENTIFIER", "").strip()
    password = os.environ.get("POPFEED_PASSWORD", "").strip()
    pds_url = (os.environ.get("POPFEED_PDS_URL") or "https://eurosky.social").strip()
    if not identifier or not password:
        print("POPFEED_IDENTIFIER and POPFEED_PASSWORD must be set.", file=sys.stderr)
        sys.exit(1)

    with AtProtoClient(pds_url) as atproto:
        atproto.create_session(identifier=identifier, password=password)
        did = atproto.session.did

        fixed = 0
        scanned = 0
        for record in atproto.iter_all_records(did, _COLLECTION_LIST_ITEM):
            scanned += 1
            value: dict = record.get("value", {})
            new_status = corrected_status(value.get("status", ""))
            if new_status is None:
                continue

            rkey = record["uri"].split("/")[-1]
            title = value.get("title", rkey)
            if args.dry_run:
                logger.info(
                    "[dry-run] Would fix %r: %r -> %r", title, value["status"], new_status
                )
                fixed += 1
                continue

            value["status"] = new_status
            atproto.put_record(
                did=did, collection=_COLLECTION_LIST_ITEM, rkey=rkey, record=value
            )
            logger.info("Fixed %r: status -> %r", title, new_status)
            fixed += 1

    logger.info("Done. %d/%d record(s) fixed.", fixed, scanned)


if __name__ == "__main__":
    main()
