import httpx
import pytest

from simple_salesforce_mcp.sf_client import SalesforceApiError, SalesforceClient
from simple_salesforce_mcp.server import dispatch_tool, run_with_auth_retry

from conftest import json_response, write_sfdx_store


def _query_handler(client, arguments):
    return str(client.query("SELECT Id FROM Account")["totalSize"])


def test_401_rereads_auth_files_and_retries_once(fake_home):
    calls = []

    def transport_handler(request):
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            # Simulate the platform rewriting the auth store with a fresh token
            # while this first request fails.
            write_sfdx_store(fake_home, token="FRESH_TOKEN")
            return json_response(
                [{"message": "expired", "errorCode": "INVALID_SESSION_ID"}], status=401
            )
        return json_response({"totalSize": 7, "done": True, "records": []})

    def client_factory(creds):
        return SalesforceClient(creds, transport=httpx.MockTransport(transport_handler))

    result = run_with_auth_retry(_query_handler, {}, client_factory=client_factory)
    assert result == "7"
    assert calls == ["Bearer 00Dxx0000000001!AQEAQtoken", "Bearer FRESH_TOKEN"]


def test_second_401_surfaces_reconnect_guidance(fake_home):
    def transport_handler(request):
        return json_response(
            [{"message": "expired", "errorCode": "INVALID_SESSION_ID"}], status=401
        )

    def client_factory(creds):
        return SalesforceClient(creds, transport=httpx.MockTransport(transport_handler))

    with pytest.raises(SalesforceApiError) as exc_info:
        run_with_auth_retry(_query_handler, {}, client_factory=client_factory)
    assert "reconnect" in exc_info.value.for_model()


def test_non_auth_errors_do_not_retry(fake_home):
    calls = []

    def transport_handler(request):
        calls.append(1)
        return json_response(
            [{"message": "bad query", "errorCode": "MALFORMED_QUERY"}], status=400
        )

    def client_factory(creds):
        return SalesforceClient(creds, transport=httpx.MockTransport(transport_handler))

    with pytest.raises(SalesforceApiError):
        run_with_auth_retry(_query_handler, {}, client_factory=client_factory)
    assert len(calls) == 1


def test_dispatch_without_credentials_returns_actionable_error(clean_env):
    text = dispatch_tool("run_soql_query", {"query": "SELECT Id FROM Account"})
    assert text.startswith("ERROR: No Salesforce credentials found")
    assert "SALESFORCE_ACCESS_TOKEN" in text


def test_dispatch_unknown_tool(clean_env):
    text = dispatch_tool("does_not_exist", {})
    assert text.startswith("ERROR: unknown tool")
    assert "run_soql_query" in text
