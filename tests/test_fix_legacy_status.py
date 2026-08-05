"""Self-check for the legacy status literal correction logic.

Run directly: python tests/test_fix_legacy_status.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fix_legacy_status import corrected_status  # noqa: E402


def test_fixes_legacy_bare_word_statuses() -> None:
    assert corrected_status("finished") == "#finished"
    assert corrected_status("in_progress") == "#in_progress"


def test_leaves_already_correct_statuses_alone() -> None:
    assert corrected_status("#finished") is None
    assert corrected_status("#in_progress") is None
    assert corrected_status("") is None


if __name__ == "__main__":
    test_fixes_legacy_bare_word_statuses()
    test_leaves_already_correct_statuses_alone()
    print("ok")
