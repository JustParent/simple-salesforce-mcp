import json

import pytest

from simple_salesforce_mcp.auth import (
    Credentials,
    CredentialsError,
    resolve_credentials,
)

from conftest import TEST_INSTANCE, TEST_TOKEN, TEST_USERNAME, write_sfdx_store


def test_resolves_from_target_org(fake_home):
    creds = resolve_credentials()
    assert creds.access_token == TEST_TOKEN
    assert creds.instance_url == TEST_INSTANCE
    assert creds.username == TEST_USERNAME
    assert creds.source == f"sfdx:{fake_home}/.sfdx/{TEST_USERNAME}.json"


def test_falls_back_to_sfdx_config(fake_home):
    (fake_home / ".sf" / "config.json").unlink()
    creds = resolve_credentials()
    assert creds.username == TEST_USERNAME


def test_falls_back_to_single_auth_file(fake_home):
    (fake_home / ".sf" / "config.json").unlink()
    (fake_home / ".sfdx" / "sfdx-config.json").unlink()
    creds = resolve_credentials()
    assert creds.username == TEST_USERNAME


def test_multiple_auth_files_without_default_is_an_error(fake_home):
    (fake_home / ".sf" / "config.json").unlink()
    (fake_home / ".sfdx" / "sfdx-config.json").unlink()
    write_sfdx_store(fake_home, username="other@example.org")
    (fake_home / ".sf" / "config.json").unlink()
    (fake_home / ".sfdx" / "sfdx-config.json").unlink()
    with pytest.raises(CredentialsError) as exc_info:
        resolve_credentials()
    message = str(exc_info.value)
    assert "other@example.org" in message
    assert TEST_USERNAME in message


def test_env_wins_over_ambiguous_store(fake_home, monkeypatch):
    write_sfdx_store(fake_home, username="other@example.org")
    (fake_home / ".sf" / "config.json").unlink()
    (fake_home / ".sfdx" / "sfdx-config.json").unlink()
    monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "envtoken")
    monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "https://env.my.salesforce.com")
    creds = resolve_credentials()
    assert creds.source == "env"
    assert creds.access_token == "envtoken"


def test_env_fallback_and_url_normalization(clean_env, monkeypatch):
    monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "envtoken")
    monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "env.my.salesforce.com/")
    creds = resolve_credentials()
    assert creds.instance_url == "https://env.my.salesforce.com"
    assert creds.username is None


def test_missing_everything_raises_actionable_error(clean_env):
    with pytest.raises(CredentialsError) as exc_info:
        resolve_credentials()
    message = str(exc_info.value)
    assert "SALESFORCE_ACCESS_TOKEN" in message
    assert ".sfdx" in message


def test_refresh_material_is_ignored(fake_home):
    write_sfdx_store(
        fake_home,
        extra={"refreshToken": "SECRET_REFRESH", "clientSecret": "SECRET_CLIENT"},
    )
    creds = resolve_credentials()
    assert creds.access_token == TEST_TOKEN
    for value in vars(creds).values():
        assert value != "SECRET_REFRESH"
        assert value != "SECRET_CLIENT"
    assert set(vars(creds)) == {
        "access_token",
        "instance_url",
        "username",
        "org_id",
        "source",
    }


def test_org_id_read_when_present(fake_home):
    write_sfdx_store(fake_home, extra={"orgId": "00Dxx0000000001EAA"})
    assert resolve_credentials().org_id == "00Dxx0000000001EAA"


def test_configured_but_missing_auth_file_falls_through(fake_home):
    (fake_home / ".sfdx" / f"{TEST_USERNAME}.json").unlink()
    with pytest.raises(CredentialsError):
        resolve_credentials()


def test_credentials_is_frozen():
    creds = Credentials(access_token="t", instance_url="https://x")
    with pytest.raises(Exception):
        creds.access_token = "other"
