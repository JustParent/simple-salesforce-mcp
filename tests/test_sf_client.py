import json

import httpx
import pytest
from conftest import json_response, parse_multipart

from simple_salesforce_mcp.sf_client import (
    SalesforceApiError,
    SalesforceAuthError,
    resolve_api_version,
)


def test_error_normalization_from_array_body(make_client):
    def handler(request):
        return json_response(
            [
                {
                    "message": "No such column 'Foo' on entity 'Account'",
                    "errorCode": "INVALID_FIELD",
                    "fields": [],
                }
            ],
            status=400,
        )

    with make_client(handler) as client, pytest.raises(SalesforceApiError) as exc_info:
        client.query("SELECT Foo FROM Account")
    err = exc_info.value
    assert err.status == 400
    assert err.error_code == "INVALID_FIELD"
    assert "describe_object" in err.for_model()


def test_required_fields_passed_through(make_client):
    def handler(request):
        return json_response(
            [
                {
                    "message": "Required fields are missing",
                    "errorCode": "REQUIRED_FIELD_MISSING",
                    "fields": ["LastName"],
                }
            ],
            status=400,
        )

    with make_client(handler) as client, pytest.raises(SalesforceApiError) as exc_info:
        client.create("Contact", {"FirstName": "A"})
    assert "LastName" in exc_info.value.for_model()


def test_401_is_auth_error(make_client):
    def handler(request):
        return json_response(
            [{"message": "Session expired", "errorCode": "INVALID_SESSION_ID"}],
            status=401,
        )

    with make_client(handler) as client, pytest.raises(SalesforceAuthError):
        client.query("SELECT Id FROM Account")


def test_invalid_session_without_401_is_auth_error(make_client):
    def handler(request):
        return json_response(
            [{"message": "Session expired", "errorCode": "INVALID_SESSION_ID"}],
            status=400,
        )

    with make_client(handler) as client, pytest.raises(SalesforceAuthError):
        client.query("SELECT Id FROM Account")


def test_non_json_error_body(make_client):
    def handler(request):
        return httpx.Response(502, text="<html>Bad gateway</html>")

    with make_client(handler) as client, pytest.raises(SalesforceApiError) as exc_info:
        client.query("SELECT Id FROM Account")
    assert "502" in exc_info.value.for_model()


def test_next_url_must_be_data_path(make_client):
    with make_client(lambda request: json_response({})) as client:
        with pytest.raises(SalesforceApiError):
            client.query_next("https://evil.example.com/steal")
        with pytest.raises(SalesforceApiError):
            client.query_next("/services/apexrest/custom")


def test_path_segments_are_validated(make_client):
    with make_client(lambda request: json_response({})) as client:
        with pytest.raises(SalesforceApiError):
            client.get_sobject("Account/../../secret", "001xx000003DGb2AAG")
        with pytest.raises(SalesforceApiError):
            client.delete("Account", "001?x=1")
        with pytest.raises(SalesforceApiError):
            client.get_sobject("Account", "001xx", fields=["Name,Id&q=x"])


def test_update_sends_patch_and_accepts_204(make_client):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(204)

    with make_client(handler) as client:
        client.update("Account", "001xx000003DGb2AAG", {"Name": "New"})
    assert seen["method"] == "PATCH"
    assert seen["path"].endswith("/sobjects/Account/001xx000003DGb2AAG")
    assert b"New" in seen["body"]


def test_upload_content_version_sends_file_and_metadata_as_multipart(make_client, tmp_path):
    blob = bytes(range(256)) * 50
    path = tmp_path / "scan.bin"
    path.write_bytes(blob)
    seen = {}

    def handler(request):
        seen["request"] = request
        return json_response({"id": "068xx0000000001AAA", "success": True, "errors": []}, 201)

    with make_client(handler) as client, path.open("rb") as fh:
        created = client.upload_content_version(
            fh, title="Scan", file_name="scan.bin", parent_id="001xx000003DGb2AAG"
        )

    request = seen["request"]
    assert created["id"] == "068xx0000000001AAA"
    assert request.method == "POST"
    assert request.url.path.endswith("/sobjects/ContentVersion/")
    # The body is sized up front rather than chunked, which Salesforce needs.
    assert int(request.headers["content-length"]) == len(request.content)
    parts = parse_multipart(request)
    assert list(parts) == ["entity_content", "VersionData"]  # metadata must come first
    meta_headers, meta = parts["entity_content"]
    assert meta_headers["content-type"] == "application/json"
    assert json.loads(meta) == {
        "Title": "Scan",
        "PathOnClient": "scan.bin",
        "FirstPublishLocationId": "001xx000003DGb2AAG",
    }
    file_headers, content = parts["VersionData"]
    assert 'filename="scan.bin"' in file_headers["content-disposition"]
    assert content == blob


def test_upload_content_version_surfaces_salesforce_errors(make_client, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"x")

    def handler(request):
        return json_response(
            [{"message": "no access", "errorCode": "INSUFFICIENT_ACCESS_OR_READONLY"}], status=403
        )

    with (
        make_client(handler) as client,
        path.open("rb") as fh,
        pytest.raises(SalesforceApiError) as exc_info,
    ):
        client.upload_content_version(
            fh, title="a", file_name="a.txt", parent_id="001xx000003DGb2AAG"
        )
    assert exc_info.value.error_code == "INSUFFICIENT_ACCESS_OR_READONLY"
    assert exc_info.value.status == 403


def test_bearer_header_and_api_version_path(make_client):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return json_response({"totalSize": 0, "done": True, "records": []})

    with make_client(handler) as client:
        client.query("SELECT Id FROM Account")
    assert seen["auth"].startswith("Bearer ")
    assert seen["path"].startswith("/services/data/v")


def test_api_version_env_override(monkeypatch):
    monkeypatch.setenv("SALESFORCE_API_VERSION", "v64.0")
    assert resolve_api_version() == "64.0"
    monkeypatch.setenv("SALESFORCE_API_VERSION", "63.0")
    assert resolve_api_version() == "63.0"
    monkeypatch.delenv("SALESFORCE_API_VERSION")
    assert resolve_api_version() == "62.0"


def test_userinfo_degrades_to_none(make_client):
    def handler(request):
        return httpx.Response(403, text="")

    with make_client(handler) as client:
        assert client.userinfo() is None
