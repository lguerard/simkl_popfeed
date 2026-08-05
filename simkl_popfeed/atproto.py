"""AT Protocol XRPC client."""

import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

_SESSION_COLLECTION_LIMIT = 100

# Retry behaviour matches jellyfin_popfeed's PopfeedAtProtoClient.SendWithRetriesAsync
# exactly: up to 6 attempts, retrying on 429/5xx, honoring a Retry-After header
# (seconds or HTTP-date) or a "wait for Ns" pattern in the error body, falling
# back to exponential backoff capped at 60s. Needed because a large batch (e.g.
# scripts/fix_legacy_status.py rewriting hundreds of records) reliably hits the
# PDS's rate limit partway through otherwise.
_MAX_REQUEST_ATTEMPTS = 6
_WAIT_FOR_RE = re.compile(r"wait\s+for\s+(\d+)\s*s", re.IGNORECASE)


def _is_record_not_found(response: httpx.Response) -> bool:
    """Return True when a response means "record does not exist".

    A missing record surfaces as HTTP 404 on some PDS implementations and as
    HTTP 400 with an ``error: "RecordNotFound"`` body on others (e.g. the
    reference Bluesky PDS).

    Parameters:
        response (httpx.Response): The XRPC response to inspect.

    Returns:
        bool: True when the record is absent.
    """
    if response.status_code == 404:
        return True
    if response.status_code == 400:
        try:
            return response.json().get("error") == "RecordNotFound"
        except Exception:
            return False
    return False


def _is_retryable(status_code: int) -> bool:
    """Return True for transient errors worth retrying (429, 5xx)."""
    return status_code == 429 or status_code >= 500


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Work out how long to wait before retrying a rate-limited/5xx request.

    Tries, in order: the ``Retry-After`` header (seconds or HTTP-date), a
    "wait for Ns" pattern in the response body (seen in some PDS rate-limit
    messages), then exponential backoff (``2**attempt``, capped at 60s).

    Parameters:
        response (httpx.Response): The failed response.
        attempt (int): The attempt number that just failed (1-indexed).

    Returns:
        float: Seconds to wait before the next attempt.
    """
    retry_after = response.headers.get("retry-after", "").strip()
    if retry_after:
        if retry_after.isdigit():
            seconds = int(retry_after)
            if seconds > 0:
                return seconds
        else:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delta = (retry_at - datetime.now(timezone.utc)).total_seconds()
                if delta > 0:
                    return delta
            except (TypeError, ValueError):
                pass

    match = _WAIT_FOR_RE.search(response.text)
    if match:
        seconds = int(match.group(1))
        if seconds > 0:
            return seconds + 1

    return min(60, 2**attempt)


class AtProtoError(Exception):
    """Raised when an AT Protocol request fails."""


class AtProtoSession:
    """Holds an authenticated AT Protocol session.

    Parameters:
        did (str): The account DID.
        handle (str): The account handle.
        access_jwt (str): Bearer access token.
        pds_url (str): PDS base URL.
    """

    def __init__(
        self,
        did: str,
        handle: str,
        access_jwt: str,
        pds_url: str,
    ) -> None:
        """Initialise the session.

        Parameters:
            did (str): The account DID.
            handle (str): The account handle.
            access_jwt (str): Bearer access token.
            pds_url (str): PDS base URL.
        """
        self.did = did
        self.handle = handle
        self.access_jwt = access_jwt
        self.pds_url = pds_url.rstrip("/")


class AtProtoClient:
    """Low-level AT Protocol XRPC client.

    Parameters:
        pds_url (str): Base URL of the PDS (e.g. https://bsky.social).
    """

    def __init__(self, pds_url: str) -> None:
        """Initialise the client without authenticating.

        Parameters:
            pds_url (str): Base URL of the PDS.
        """
        self._pds_url = pds_url.rstrip("/")
        self._http = httpx.Client(timeout=30.0)
        self._session: Optional[AtProtoSession] = None

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "AtProtoClient":
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close the HTTP client on context exit."""
        self.close()

    @property
    def session(self) -> AtProtoSession:
        """Return the active session, raising if not authenticated.

        Returns:
            AtProtoSession: The active session.

        Raises:
            AtProtoError: If not yet authenticated.
        """
        if self._session is None:
            raise AtProtoError("Not authenticated; call create_session first")
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization headers for authenticated requests.

        Returns:
            dict[str, str]: HTTP headers with Bearer token.
        """
        return {"Authorization": f"Bearer {self.session.access_jwt}"}

    def _raise_for_error(self, response: httpx.Response) -> None:
        """Raise AtProtoError for non-2xx responses.

        Parameters:
            response (httpx.Response): The HTTP response to check.

        Raises:
            AtProtoError: If the response status is an error.
        """
        if response.is_error:
            try:
                body = response.json()
                message = body.get("message") or body.get("error") or ""
            except Exception:
                message = response.text[:200]
            raise AtProtoError(f"XRPC error {response.status_code}: {message}")

    def _send_with_retries(
        self, send: Callable[[], httpx.Response], operation: str
    ) -> httpx.Response:
        """Send a request, retrying transient failures with backoff.

        Returns the response as-is on success OR on a non-retryable error
        (404s from a missing record, 400s, etc.) — callers keep doing their
        own success/error handling on the result exactly as before; this
        only adds a retry loop around 429/5xx responses.

        Parameters:
            send (Callable[[], httpx.Response]): Issues one HTTP request.
            operation (str): XRPC method name, for logging.

        Returns:
            httpx.Response: The final response.

        Raises:
            AtProtoError: On a network-level failure. A persistent 429/5xx
                that survives every retry is NOT raised here — it's
                returned like any other response, and surfaces through
                the caller's normal ``_raise_for_error``/not-found
                handling instead, same as before retries existed.
        """
        for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
            try:
                response = send()
            except httpx.RequestError as exc:
                raise AtProtoError(f"{operation} request failed: {exc}") from exc

            if response.is_success or not _is_retryable(response.status_code):
                return response
            if attempt == _MAX_REQUEST_ATTEMPTS:
                return response

            delay = _retry_delay(response, attempt)
            logger.warning(
                "ATProto %s failed with %d on attempt %d/%d. Waiting %.0fs before retry.",
                operation,
                response.status_code,
                attempt,
                _MAX_REQUEST_ATTEMPTS,
                delay,
            )
            time.sleep(delay)

        raise AssertionError("unreachable")  # loop always returns above

    def create_session(self, identifier: str, password: str) -> AtProtoSession:
        """Authenticate and store a session.

        Parameters:
            identifier (str): Handle or DID.
            password (str): Account password or app password.

        Returns:
            AtProtoSession: The newly created session.

        Raises:
            AtProtoError: On authentication failure.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.server.createSession"
        response = self._send_with_retries(
            lambda: self._http.post(
                url, json={"identifier": identifier, "password": password}
            ),
            "createSession",
        )
        self._raise_for_error(response)
        data: dict = response.json()
        self._session = AtProtoSession(
            did=data["did"],
            handle=data["handle"],
            access_jwt=data["accessJwt"],
            pds_url=self._pds_url,
        )
        logger.info("Authenticated as %s (%s)", data["handle"], data["did"])
        return self._session

    def list_records(
        self,
        did: str,
        collection: str,
        limit: int = _SESSION_COLLECTION_LIMIT,
        cursor: Optional[str] = None,
    ) -> dict:
        """Fetch a page of records from a collection.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.
            limit (int): Maximum records to return.
            cursor (Optional[str]): Pagination cursor.

        Returns:
            dict: Raw XRPC response (``records`` + optional ``cursor``).

        Raises:
            AtProtoError: On request failure.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.repo.listRecords"
        params: dict[str, Any] = {
            "repo": did,
            "collection": collection,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        response = self._send_with_retries(
            lambda: self._http.get(url, params=params, headers=self._auth_headers()),
            "listRecords",
        )
        self._raise_for_error(response)
        return response.json()

    def iter_all_records(self, did: str, collection: str) -> Iterator[dict]:
        """Yield every record in a collection, handling pagination.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.

        Yields:
            dict: Individual record objects (``{ uri, cid, value }``).
        """
        cursor: Optional[str] = None
        while True:
            page = self.list_records(
                did=did,
                collection=collection,
                cursor=cursor,
            )
            records: list[dict] = page.get("records", [])
            for record in records:
                yield record
            cursor = page.get("cursor")
            if not cursor:
                break

    def create_record(self, did: str, collection: str, record: dict) -> dict:
        """Create a new record in a collection.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.
            record (dict): The record value to store.

        Returns:
            dict: XRPC response containing ``uri`` and ``cid``.

        Raises:
            AtProtoError: On request failure.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.repo.createRecord"
        response = self._send_with_retries(
            lambda: self._http.post(
                url,
                json={"repo": did, "collection": collection, "record": record},
                headers=self._auth_headers(),
            ),
            "createRecord",
        )
        self._raise_for_error(response)
        return response.json()

    def get_record(
        self, did: str, collection: str, rkey: str
    ) -> Optional[dict]:
        """Fetch a single record by its deterministic rkey.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.
            rkey (str): The record key.

        Returns:
            Optional[dict]: The record (``{ uri, cid, value }``), or ``None``
                when the record does not exist.

        Raises:
            AtProtoError: On request failure other than a missing record.

        Note:
            A missing record returns HTTP 404 on some PDS implementations and
            HTTP 400 with ``error: "RecordNotFound"`` on others (e.g. the
            reference Bluesky PDS); both are treated as absent.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.repo.getRecord"
        params = {"repo": did, "collection": collection, "rkey": rkey}
        response = self._send_with_retries(
            lambda: self._http.get(url, params=params, headers=self._auth_headers()),
            "getRecord",
        )
        if _is_record_not_found(response):
            return None
        self._raise_for_error(response)
        return response.json()

    def delete_record(self, did: str, collection: str, rkey: str) -> None:
        """Delete a record by rkey. A missing record is treated as success.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.
            rkey (str): The record key.

        Raises:
            AtProtoError: On request failure other than a missing record.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.repo.deleteRecord"
        response = self._send_with_retries(
            lambda: self._http.post(
                url,
                json={"repo": did, "collection": collection, "rkey": rkey},
                headers=self._auth_headers(),
            ),
            "deleteRecord",
        )
        if _is_record_not_found(response):
            return
        self._raise_for_error(response)

    def put_record(self, did: str, collection: str, rkey: str, record: dict) -> dict:
        """Create or replace a record at a specific rkey.

        Parameters:
            did (str): The repo DID.
            collection (str): The collection NSID.
            rkey (str): The record key.
            record (dict): The record value to store.

        Returns:
            dict: XRPC response containing ``uri`` and ``cid``.

        Raises:
            AtProtoError: On request failure.
        """
        url = f"{self._pds_url}/xrpc/com.atproto.repo.putRecord"
        response = self._send_with_retries(
            lambda: self._http.post(
                url,
                json={
                    "repo": did,
                    "collection": collection,
                    "rkey": rkey,
                    "record": record,
                },
                headers=self._auth_headers(),
            ),
            "putRecord",
        )
        self._raise_for_error(response)
        return response.json()
