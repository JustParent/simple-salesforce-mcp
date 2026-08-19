"""Thin Salesforce REST API client on httpx.

Deliberately minimal: the tool surface only needs SOQL query, parameterized
search, single-record CRUD, describe, the sobjects list, and userinfo. Tools
depend only on this module's public surface, so the transport layer can be
swapped without touching them.
"""

from __future__ import annotations

import os
import re

import httpx

from .auth import Credentials

DEFAULT_API_VERSION = "62.0"

# Total budget must fit inside the platform's per-tool-call RPC timeout
# (30s default) with margin for bridge overhead.
REQUEST_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_]+")
_FIELD_NAME_RE = re.compile(r"[A-Za-z0-9_.]+")

HINTS = {
    "MALFORMED_QUERY": (
        "Check the SOQL syntax and the exact API names with describe_object / list_objects."
    ),
    "INVALID_FIELD": "Check exact field API names with describe_object('<Object>').",
    "INVALID_TYPE": (
        "Check the object API name with list_objects (custom objects end in __c)."
    ),
    "INVALID_FIELD_FOR_INSERT_UPDATE": (
        "This field is not writable; describe_object shows which fields are "
        "createable/updateable."
    ),
    "NOT_FOUND": (
        "Check the record Id (15 or 18 characters) and that object_type is the exact "
        "API name."
    ),
    "REQUIRED_FIELD_MISSING": "describe_object shows which fields are required.",
    "INSUFFICIENT_ACCESS_OR_READONLY": (
        "The connected user's Salesforce permissions block this; surface this to the "
        "user rather than working around it."
    ),
    "INSUFFICIENT_ACCESS": (
        "The connected user's Salesforce permissions block this; surface this to the "
        "user rather than working around it."
    ),
}


class SalesforceApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        error_code: str = "",
        fields: list[str] | None = None,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_code = error_code
        self.fields = fields or []
        self.hint = hint

    def for_model(self) -> str:
        parts = [f"{self.error_code}: {self.message}" if self.error_code else self.message]
        if self.fields:
            parts.append(f"Fields: {', '.join(self.fields)}.")
        hint = self.hint or HINTS.get(self.error_code)
        if hint:
            parts.append(f"Hint: {hint}")
        return " ".join(parts)


class SalesforceAuthError(SalesforceApiError):
    """HTTP 401 or INVALID_SESSION_ID — the access token was rejected."""


def resolve_api_version() -> str:
    raw = os.environ.get("SALESFORCE_API_VERSION", "").strip()
    if not raw:
        return DEFAULT_API_VERSION
    return raw[1:] if raw[:1].lower() == "v" else raw


class SalesforceClient:
    def __init__(
        self,
        creds: Credentials,
        api_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.creds = creds
        self.api_version = api_version or resolve_api_version()
        self.data_path = f"/services/data/v{self.api_version}"
        self._http = httpx.Client(
            base_url=creds.instance_url,
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SalesforceClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- public API ---------------------------------------------------------

    def query(self, soql: str) -> dict:
        return self._request("GET", f"{self.data_path}/query/", params={"q": soql}).json()

    def query_next(self, next_url: str) -> dict:
        # Only accept cursor paths Salesforce itself returned; keeps the model
        # from steering the client at arbitrary instance paths.
        if not next_url.startswith("/services/data/"):
            raise SalesforceApiError(
                "next_url must be the /services/data/... path returned by a previous "
                "run_soql_query call.",
                error_code="INVALID_NEXT_URL",
            )
        return self._request("GET", next_url).json()

    def get_sobject(
        self, object_type: str, record_id: str, fields: list[str] | None = None
    ) -> dict:
        params = None
        if fields:
            params = {"fields": ",".join(_valid_field(f) for f in fields)}
        return self._request(
            "GET",
            f"{self.data_path}/sobjects/{_valid_segment(object_type, 'object_type')}"
            f"/{_valid_segment(record_id, 'record_id')}",
            params=params,
        ).json()

    def create(self, object_type: str, data: dict) -> dict:
        return self._request(
            "POST",
            f"{self.data_path}/sobjects/{_valid_segment(object_type, 'object_type')}/",
            json_body=data,
        ).json()

    def update(self, object_type: str, record_id: str, data: dict) -> None:
        self._request(
            "PATCH",
            f"{self.data_path}/sobjects/{_valid_segment(object_type, 'object_type')}"
            f"/{_valid_segment(record_id, 'record_id')}",
            json_body=data,
        )

    def delete(self, object_type: str, record_id: str) -> None:
        self._request(
            "DELETE",
            f"{self.data_path}/sobjects/{_valid_segment(object_type, 'object_type')}"
            f"/{_valid_segment(record_id, 'record_id')}",
        )

    def describe(self, object_type: str) -> dict:
        return self._request(
            "GET",
            f"{self.data_path}/sobjects/{_valid_segment(object_type, 'object_type')}"
            "/describe/",
        ).json()

    def list_sobjects(self) -> dict:
        return self._request("GET", f"{self.data_path}/sobjects/").json()

    def parameterized_search(self, body: dict) -> dict:
        return self._request(
            "POST", f"{self.data_path}/parameterizedSearch/", json_body=body
        ).json()

    def userinfo(self) -> dict | None:
        """Best effort — returns None when the token lacks the id scope."""
        try:
            response = self._http.get("/services/oauth2/userinfo")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # -- internals ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        response = self._http.request(method, path, params=params, json=json_body)
        if response.status_code >= 400:
            raise _error_from_response(response)
        return response


def _valid_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not _PATH_SEGMENT_RE.fullmatch(value):
        raise SalesforceApiError(
            f"{name} must contain only letters, digits, and underscores (got {value!r}).",
            error_code="INVALID_PARAMETER",
        )
    return value


def _valid_field(value: str) -> str:
    if not isinstance(value, str) or not _FIELD_NAME_RE.fullmatch(value):
        raise SalesforceApiError(
            f"Invalid field name {value!r}: use field API names like Name or Account.Name.",
            error_code="INVALID_PARAMETER",
        )
    return value


def _error_from_response(response: httpx.Response) -> SalesforceApiError:
    status = response.status_code
    error_code = ""
    message = f"Salesforce returned HTTP {status}."
    fields: list[str] = []
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        message = str(body.get("message") or message)
        error_code = str(body.get("errorCode") or "")
        raw_fields = body.get("fields")
        if isinstance(raw_fields, list):
            fields = [str(f) for f in raw_fields]
    cls = (
        SalesforceAuthError
        if status == 401 or error_code == "INVALID_SESSION_ID"
        else SalesforceApiError
    )
    return cls(message, status=status, error_code=error_code, fields=fields)
