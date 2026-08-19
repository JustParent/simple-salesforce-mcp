import json

import httpx
import pytest

from simple_salesforce_mcp.tools.records import (
    handle_create_record,
    handle_delete_record,
    handle_get_record,
    handle_update_record,
)

from conftest import json_response


@pytest.fixture
def refusing_client(make_client):
    """A client whose transport fails the test if any HTTP request is made."""

    def handler(request):
        pytest.fail(f"unexpected HTTP call: {request.method} {request.url}")

    return make_client(handler)


def test_get_record_passes_fields(make_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["fields"] = request.url.params.get("fields")
        return json_response(
            {"attributes": {"type": "Account"}, "Id": "001", "Name": "Acme"}
        )

    with make_client(handler) as client:
        payload = json.loads(
            handle_get_record(
                client,
                {"object_type": "Account", "record_id": "001", "fields": ["Id", "Name"]},
            )
        )
    assert seen["path"].endswith("/sobjects/Account/001")
    assert seen["fields"] == "Id,Name"
    assert payload == {"Id": "001", "Name": "Acme"}


def test_create_record(make_client):
    def handler(request):
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"Name": "Acme"}
        return json_response({"id": "001NEW", "success": True, "errors": []}, status=201)

    with make_client(handler) as client:
        text = handle_create_record(
            client, {"object_type": "Account", "data": {"Name": "Acme"}}
        )
    assert text == "Created Account 001NEW."


def test_create_requires_non_empty_data(refusing_client):
    with refusing_client as client:
        assert handle_create_record(
            client, {"object_type": "Account", "data": {}}
        ).startswith("ERROR:")


def test_update_without_confirm_is_blocked_and_makes_no_call(refusing_client):
    with refusing_client as client:
        text = handle_update_record(
            client,
            {"object_type": "Account", "record_id": "001", "data": {"Name": "New"}},
        )
    assert text.startswith("CONFIRMATION REQUIRED")
    assert "no changes were made" in text
    assert "confirm=true" in text
    assert "Account 001" in text
    assert '"Name":"New"' in text


def test_update_with_string_true_is_still_blocked(refusing_client):
    with refusing_client as client:
        text = handle_update_record(
            client,
            {
                "object_type": "Account",
                "record_id": "001",
                "data": {"Name": "New"},
                "confirm": "true",
            },
        )
    assert text.startswith("CONFIRMATION REQUIRED")


def test_update_with_confirm_patches_and_strips_id(make_client):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    with make_client(handler) as client:
        text = handle_update_record(
            client,
            {
                "object_type": "Account",
                "record_id": "001",
                "data": {"Id": "001", "attributes": {}, "Name": "New"},
                "confirm": True,
            },
        )
    assert seen["method"] == "PATCH"
    assert seen["body"] == {"Name": "New"}
    assert text.startswith("Updated Account 001")


def test_delete_without_confirm_is_blocked(refusing_client):
    with refusing_client as client:
        text = handle_delete_record(
            client, {"object_type": "Account", "record_id": "001"}
        )
    assert text.startswith("CONFIRMATION REQUIRED")
    assert "permanently delete Account 001" in text


def test_delete_with_confirm(make_client):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        return httpx.Response(204)

    with make_client(handler) as client:
        text = handle_delete_record(
            client, {"object_type": "Account", "record_id": "001", "confirm": True}
        )
    assert seen["method"] == "DELETE"
    assert text == "Deleted Account 001."


def test_missing_params_reported(refusing_client):
    with refusing_client as client:
        assert handle_get_record(client, {"object_type": "Account"}).startswith(
            "ERROR: missing required parameter"
        )
