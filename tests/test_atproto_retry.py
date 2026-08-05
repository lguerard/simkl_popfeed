"""Self-check for AtProtoClient's retry/backoff logic (matches jellyfin_popfeed's
PopfeedAtProtoClient.SendWithRetriesAsync: retry 429/5xx, honor Retry-After
or a "wait for Ns" body pattern, else exponential backoff).

Run directly: python tests/test_atproto_retry.py
"""

import httpx

import simkl_popfeed.atproto as atproto_module
from simkl_popfeed.atproto import AtProtoClient, _is_retryable, _retry_delay


def test_is_retryable_covers_429_and_5xx_only() -> None:
    assert _is_retryable(429) is True
    assert _is_retryable(500) is True
    assert _is_retryable(503) is True
    assert _is_retryable(400) is False
    assert _is_retryable(404) is False


def test_retry_delay_prefers_retry_after_seconds_header() -> None:
    response = httpx.Response(429, headers={"retry-after": "5"}, text="")
    assert _retry_delay(response, attempt=1) == 5


def test_retry_delay_falls_back_to_wait_for_pattern_in_body() -> None:
    response = httpx.Response(429, text="Rate limited, wait for 12s and retry")
    assert _retry_delay(response, attempt=1) == 13


def test_retry_delay_falls_back_to_exponential_backoff() -> None:
    response = httpx.Response(500, text="Internal Server Error")
    assert _retry_delay(response, attempt=1) == 2
    assert _retry_delay(response, attempt=3) == 8
    assert _retry_delay(response, attempt=10) == 60  # capped


def test_client_retries_429_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(
            200, json={"did": "did:plc:fake", "handle": "x", "accessJwt": "y"}
        )

    client = AtProtoClient("https://example.test")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    # Don't actually sleep in a test — behavior under test is "did it retry
    # and eventually succeed", not the real timing.
    original_sleep = atproto_module.time.sleep
    atproto_module.time.sleep = lambda _seconds: None
    try:
        session = client.create_session("x", "y")
    finally:
        atproto_module.time.sleep = original_sleep

    assert attempts["count"] == 3
    assert session.did == "did:plc:fake"


if __name__ == "__main__":
    test_is_retryable_covers_429_and_5xx_only()
    test_retry_delay_prefers_retry_after_seconds_header()
    test_retry_delay_falls_back_to_wait_for_pattern_in_body()
    test_retry_delay_falls_back_to_exponential_backoff()
    test_client_retries_429_then_succeeds()
    print("ok")
