"""Self-check for the SeriesGuide export parser used by the migration script.

Run directly: python tests/test_migration_parsing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from migrate_seriesguide import build_movie_payloads, build_show_payloads  # noqa: E402


def test_build_movie_payloads_skips_unwatched_and_missing_tmdb_id() -> None:
    export = {
        "movies": [
            {"tmdb_id": 603, "title": "The Matrix", "watched": True},
            {"tmdb_id": 604, "title": "Not watched", "watched": False},
            {"title": "No TMDb id", "watched": True},
        ]
    }
    payloads, skipped = build_movie_payloads(export)
    assert payloads == [{"ids": {"tmdb": 603}, "status": "completed"}]
    assert skipped == 1


def test_build_show_payloads_groups_episodes_by_show() -> None:
    export = {
        "shows": [
            {
                "tmdb_id": 1399,
                "title": "Game of Thrones",
                "seasons": [
                    {
                        "season": 1,
                        "episodes": [
                            {"episode": 1, "watched": True},
                            {"episode": 2, "watched": False},
                        ],
                    }
                ],
            }
        ]
    }
    payloads, skipped = build_show_payloads(export)
    assert skipped == 0
    assert payloads == [
        {
            "ids": {"tmdb": 1399},
            "seasons": [{"number": 1, "episodes": [{"number": 1}]}],
        }
    ]


if __name__ == "__main__":
    test_build_movie_payloads_skips_unwatched_and_missing_tmdb_id()
    test_build_show_payloads_groups_episodes_by_show()
    print("ok")
