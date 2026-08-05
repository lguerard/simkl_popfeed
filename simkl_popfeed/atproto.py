"""AT Protocol XRPC client."""

import logging
from typing import Any, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

_SESSION_COLLECTION_LIMIT = 100


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
        try:
            response = self._http.post(
                url,
                json={"identifier": identifier, "password": password},
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"Session request failed: {exc}") from exc
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
        try:
            response = self._http.get(
                url,
                params=params,
                headers=self._auth_headers(),
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"listRecords request failed: {exc}") from exc
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
        try:
            response = self._http.post(
                url,
                json={
                    "repo": did,
                    "collection": collection,
                    "record": record,
                },
                headers=self._auth_headers(),
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"createRecord request failed: {exc}") from exc
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
        try:
            response = self._http.get(
                url,
                params=params,
                headers=self._auth_headers(),
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"getRecord request failed: {exc}") from exc
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
        try:
            response = self._http.post(
                url,
                json={"repo": did, "collection": collection, "rkey": rkey},
                headers=self._auth_headers(),
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"deleteRecord request failed: {exc}") from exc
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
        try:
            response = self._http.post(
                url,
                json={
                    "repo": did,
                    "collection": collection,
                    "rkey": rkey,
                    "record": record,
                },
                headers=self._auth_headers(),
            )
        except httpx.RequestError as exc:
            raise AtProtoError(f"putRecord request failed: {exc}") from exc
        self._raise_for_error(response)
        return response.json()
