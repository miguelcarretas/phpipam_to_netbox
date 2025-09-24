"""Client helpers for interacting with the phpIPAM REST API."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
import logging
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request

logger = logging.getLogger(__name__)


class PhpIPAMError(RuntimeError):
    """Base exception raised for API related issues."""


class PhpIPAMAuthenticationError(PhpIPAMError):
    """Raised when authentication with phpIPAM fails."""


class PhpIPAMNotFoundError(PhpIPAMError):
    """Raised when the requested object is not found."""


class _HttpResponse:
    """Lightweight wrapper mimicking the :mod:`requests` response object."""

    def __init__(self, *, status: int, reason: str, data: bytes, headers: Dict[str, str]):
        self.status_code = status
        self.reason = reason
        self._data = data
        self.headers = headers

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self._data:
            return {}
        return json.loads(self.text)


class HttpSession:
    """Small HTTP helper with an interface similar to :class:`requests.Session`."""

    def __init__(self, *, verify_ssl: bool = True):
        self.verify_ssl = verify_ssl
        self.headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "phpipam-to-netbox/0.1",
        }

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        data: Optional[bytes] = None,
        json_data: Optional[Any] = None,
        timeout: int = 30,
    ) -> _HttpResponse:
        if json_data is not None:
            data = json.dumps(json_data).encode("utf-8")

        req = request.Request(url, data=data, method=method.upper())
        for key, value in self.headers.items():
            req.add_header(key, value)

        context = None if self.verify_ssl else ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=timeout, context=context) as response:
                body = response.read()
                status = response.getcode()
                reason = getattr(response, "reason", "")
                headers = dict(response.headers.items())
        except error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            reason = getattr(exc, "reason", "")
            headers = dict(exc.headers.items()) if exc.headers else {}
        except error.URLError as exc:
            raise PhpIPAMError(f"Error communicating with phpIPAM: {exc}") from exc

        return _HttpResponse(status=status, reason=reason, data=body, headers=headers)

    def post(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        json_data: Optional[Any] = None,
        timeout: int = 30,
    ) -> _HttpResponse:
        return self.request("POST", url, data=data, json_data=json_data, timeout=timeout)

@dataclass
class PhpIPAMClient:
    """Simple client for the phpIPAM API.

    Parameters
    ----------
    base_url:
        Base URL for the phpIPAM deployment, e.g. ``https://phpipam.example.com``.
    app_id:
        Identifier of the API application configured in phpIPAM.
    username:
        Username used for authentication. Optional when a token is provided.
    password:
        Password used for authentication. Optional when a token is provided.
    token:
        Pre-generated API token. When omitted the client will attempt to log in
        using ``username`` and ``password`` when the first request is executed.
    verify_ssl:
        Controls SSL certificate verification. Disable with ``False`` only when
        dealing with a development system.
    session:
        Optional :class:`HttpSession` object to reuse HTTP connections.
    """

    base_url: str
    app_id: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    verify_ssl: bool = True
    session: Optional[HttpSession] = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.session = self.session or HttpSession(verify_ssl=self.verify_ssl)
        self.session.verify_ssl = self.verify_ssl
        if self.token:
            self.session.headers.setdefault("token", self.token)

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------
    def authenticate(self) -> str:
        """Authenticate against phpIPAM and return the issued token."""

        if not self.username or not self.password:
            raise PhpIPAMAuthenticationError(
                "Cannot authenticate without username and password."
            )

        login_url = f"{self.base_url}/api/{self.app_id}/user/"
        logger.debug("Authenticating against %s", login_url)
        response = self.session.post(
            login_url,
            json_data={"username": self.username, "password": self.password},
            timeout=30,
        )
        data = self._decode_response(response)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise PhpIPAMAuthenticationError(
                "Login succeeded but phpIPAM did not return a token."
            )

        self.token = token
        self.session.headers["token"] = token
        logger.debug("Authenticated successfully; token cached.")
        return token

    # ------------------------------------------------------------------
    # Public API helpers
    # ------------------------------------------------------------------
    def get_customers(self) -> List[Dict[str, Any]]:
        """Return the list of customers defined in phpIPAM."""

        try:
            data = self._request("GET", "/tools/customers/")
        except PhpIPAMNotFoundError:
            # Customer module disabled – simply return an empty list.
            logger.info("The customers module appears to be disabled in phpIPAM.")
            return []
        return list(data or [])

    def get_all_subnets(self) -> List[Dict[str, Any]]:
        """Return all subnets visible to the authenticated user."""

        data = self._request("GET", "/subnets/")
        return list(data or [])

    def get_addresses_for_subnet(self, subnet_id: str) -> List[Dict[str, Any]]:
        """Return all addresses that belong to ``subnet_id``."""

        endpoint = f"/subnets/{subnet_id}/addresses/"
        try:
            data = self._request("GET", endpoint)
        except PhpIPAMNotFoundError:
            # The API returns 404 when a subnet has no addresses.
            return []
        return list(data or [])

    def get_ip_tags(self) -> List[Dict[str, Any]]:
        """Return custom address tags defined in phpIPAM.

        The exact endpoint for address tags has changed between phpIPAM
        releases. The client therefore tries a couple of known locations until
        it receives a valid response.
        """

        for endpoint in ("/tools/ipTags/", "/tools/tags/"):
            try:
                data = self._request("GET", endpoint)
            except PhpIPAMError:
                logger.debug("Address tags endpoint %s not available", endpoint)
                continue
            return list(data or [])
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute a request against the phpIPAM API."""

        url = f"{self.base_url}/api/{self.app_id}{path}"
        timeout = int(kwargs.pop("timeout", 30))
        json_data = kwargs.pop("json", None)
        data = kwargs.pop("data", None)
        if kwargs:
            raise TypeError(f"Unsupported keyword arguments: {', '.join(kwargs)}")
        logger.debug("phpIPAM %s %s", method.upper(), url)

        if not self.session.headers.get("token") and (self.username and self.password):
            logger.debug("No cached token present; performing login.")
            self.authenticate()

        response = self.session.request(
            method,
            url,
            data=data,
            json_data=json_data,
            timeout=timeout,
        )

        if response.status_code == 401 and (self.username and self.password):
            # Token likely expired – retry after re-authentication.
            logger.info("Token expired; attempting to re-authenticate.")
            self.authenticate()
            response = self.session.request(
                method,
                url,
                data=data,
                json_data=json_data,
                timeout=timeout,
            )

        return self._decode_response(response)

    def _decode_response(self, response: _HttpResponse) -> Any:
        """Validate API responses and return their payload."""

        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - depends on server payload
            raise PhpIPAMError(
                f"phpIPAM returned an invalid JSON payload: {response.text}"
            ) from exc

        message = payload.get("message") or payload.get("error") or response.reason
        success = payload.get("success", response.ok)

        if response.status_code == 401:
            raise PhpIPAMAuthenticationError(message)
        if response.status_code == 404:
            raise PhpIPAMNotFoundError(message)
        if response.status_code >= 400 or not success:
            raise PhpIPAMError(message)

        return payload.get("data", payload)


def normalise_collection(items: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return ``items`` as a list with ``None`` mapped to an empty list."""

    if not items:
        return []
    if isinstance(items, list):
        return items
    return list(items)
