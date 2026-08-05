"""Self-check for Simkl rating conversion.

Run directly: python tests/test_rating_and_review.py
"""

from simkl_popfeed.popfeed import _rating_to_popfeed


def test_rating_scale_conversion() -> None:
    assert _rating_to_popfeed(10) == 5
    assert _rating_to_popfeed(9) == 5
    assert _rating_to_popfeed(8) == 4
    assert _rating_to_popfeed(2) == 1
    assert _rating_to_popfeed(1) == 1
    assert _rating_to_popfeed(None) is None


if __name__ == "__main__":
    test_rating_scale_conversion()
    print("ok")
