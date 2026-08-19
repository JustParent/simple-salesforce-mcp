import json
from pathlib import Path

import httpx
import pytest

from simple_salesforce_mcp.auth import resolve_credentials
from simple_salesforce_mcp.sf_client import SalesforceClient

TEST_USERNAME = "user@example.org"
TEST_TOKEN = "00Dxx0000000001!AQEAQtoken"
TEST_INSTANCE = "https://example.my.salesforce.com"


def write_sfdx_store(
    home: Path,
    username: str = TEST_USERNAME,
    token: str = TEST_TOKEN,
    instance: str = TEST_INSTANCE,
    extra: dict | None = None,
) -> None:
    (home / ".sf").mkdir(parents=True, exist_ok=True)
    (home / ".sfdx").mkdir(parents=True, exist_ok=True)
    (home / ".sf" / "config.json").write_text(json.dumps({"target-org": username}))
    (home / ".sfdx" / "sfdx-config.json").write_text(
        json.dumps({"defaultusername": username})
    )
    auth = {"username": username, "accessToken": token, "instanceUrl": instance}
    if extra:
        auth.update(extra)
    (home / ".sfdx" / f"{username}.json").write_text(json.dumps(auth))


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Empty $HOME, no Salesforce env vars."""
    monkeypatch.delenv("SALESFORCE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SALESFORCE_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SALESFORCE_API_VERSION", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_home(clean_env):
    """$HOME with a materialised sfdx auth store, as the platform writes it."""
    write_sfdx_store(clean_env)
    return clean_env


@pytest.fixture
def make_client(fake_home):
    """Build a SalesforceClient whose HTTP layer is a MockTransport handler."""

    def factory(handler) -> SalesforceClient:
        creds = resolve_credentials()
        return SalesforceClient(creds, transport=httpx.MockTransport(handler))

    return factory


def json_response(data, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)
