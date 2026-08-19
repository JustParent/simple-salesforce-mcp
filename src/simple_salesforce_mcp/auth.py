"""Credential discovery for the Salesforce MCP server.

Two sources, checked in order:

1. A Salesforce CLI style auth store under ``$HOME`` — either materialised by
   the JustParent platform (``home_files`` mode) or written by a real
   ``sf org login``:

   - ``~/.sf/config.json`` → ``{"target-org": "<username>"}``
   - ``~/.sfdx/sfdx-config.json`` → ``{"defaultusername": "<username>"}``
   - ``~/.sfdx/<username>.json`` → ``{"accessToken": ..., "instanceUrl": ...}``

2. ``SALESFORCE_ACCESS_TOKEN`` + ``SALESFORCE_INSTANCE_URL`` environment
   variables, for local development and standalone use.

Credentials must be resolved on every tool call, never cached: the platform
rewrites the auth files with a freshly minted access token before each call,
so a cached token would go stale and defeat the refresh mechanism.

Refresh tokens and client secrets are never read into the credential object,
stored, or logged — a real CLI auth store contains them, this server only
ever uses the access token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Files in ~/.sfdx that are configuration, not per-org auth stores.
_NON_AUTH_SFDX_FILES = frozenset({"sfdx-config.json", "alias.json", "key.json"})

MISSING_CREDENTIALS_MESSAGE = (
    "No Salesforce credentials found. Provide either a Salesforce CLI auth store "
    "(~/.sfdx/<username>.json containing accessToken and instanceUrl, with the "
    "username named in ~/.sf/config.json under target-org) or set the "
    "SALESFORCE_ACCESS_TOKEN and SALESFORCE_INSTANCE_URL environment variables. "
    "In JustParent, the user needs to connect Salesforce under Profile → Integrations."
)


class CredentialsError(Exception):
    """No usable Salesforce credentials could be found."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    instance_url: str
    username: str | None = None
    org_id: str | None = None
    source: str = "env"


def resolve_credentials(home: Path | None = None) -> Credentials:
    home = home or Path.home()
    ambiguity: CredentialsError | None = None
    try:
        creds = _from_sfdx_store(home)
    except CredentialsError as exc:
        creds = None
        ambiguity = exc
    if creds is None:
        creds = _from_env()
    if creds is None:
        raise ambiguity or CredentialsError(MISSING_CREDENTIALS_MESSAGE)
    return creds


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _discover_username(home: Path) -> str | None:
    for path, key in (
        (home / ".sf" / "config.json", "target-org"),
        (home / ".sfdx" / "sfdx-config.json", "defaultusername"),
    ):
        config = _read_json(path)
        if config:
            username = config.get(key)
            if isinstance(username, str) and username:
                return username

    sfdx_dir = home / ".sfdx"
    if not sfdx_dir.is_dir():
        return None
    candidates = [
        p for p in sorted(sfdx_dir.glob("*.json")) if p.name not in _NON_AUTH_SFDX_FILES
    ]
    if len(candidates) == 1:
        return candidates[0].stem
    if len(candidates) > 1:
        raise CredentialsError(
            "Multiple Salesforce auth files found in ~/.sfdx and no default org is "
            "configured. Set target-org in ~/.sf/config.json to one of: "
            + ", ".join(p.stem for p in candidates)
        )
    return None


def _from_sfdx_store(home: Path) -> Credentials | None:
    username = _discover_username(home)
    if not username:
        return None
    auth_path = home / ".sfdx" / f"{username}.json"
    auth = _read_json(auth_path)
    if auth is None:
        return None
    access_token = auth.get("accessToken")
    instance_url = auth.get("instanceUrl")
    if not (isinstance(access_token, str) and access_token):
        return None
    if not (isinstance(instance_url, str) and instance_url):
        return None
    stored_username = auth.get("username")
    org_id = auth.get("orgId")
    return Credentials(
        access_token=access_token,
        instance_url=_normalize_instance_url(instance_url),
        username=stored_username if isinstance(stored_username, str) else username,
        org_id=org_id if isinstance(org_id, str) else None,
        source=f"sfdx:{auth_path}",
    )


def _from_env() -> Credentials | None:
    token = os.environ.get("SALESFORCE_ACCESS_TOKEN")
    instance = os.environ.get("SALESFORCE_INSTANCE_URL")
    if not token or not instance:
        return None
    return Credentials(
        access_token=token,
        instance_url=_normalize_instance_url(instance),
        source="env",
    )


def _normalize_instance_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url
