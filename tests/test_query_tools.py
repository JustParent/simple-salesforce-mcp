import json

from simple_salesforce_mcp.tools.query import (
    handle_run_soql_query,
    handle_search_records,
)

from conftest import json_response


def test_soql_strips_attributes_and_nested_subqueries(make_client):
    def handler(request):
        assert request.url.params["q"] == "SELECT Id FROM Account"
        return json_response(
            {
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "attributes": {"type": "Account", "url": "/x"},
                        "Id": "001",
                        "Name": "Acme",
                        "Contacts": {
                            "totalSize": 1,
                            "done": True,
                            "records": [
                                {"attributes": {"type": "Contact"}, "Id": "003"}
                            ],
                        },
                    }
                ],
            }
        )

    with make_client(handler) as client:
        payload = json.loads(
            handle_run_soql_query(client, {"query": "SELECT Id FROM Account"})
        )
    assert payload["total_size"] == 1
    assert payload["done"] is True
    record = payload["records"][0]
    assert "attributes" not in record
    assert record["Contacts"] == {
        "total_size": 1,
        "done": True,
        "records": [{"Id": "003"}],
    }


def test_soql_pagination_round_trip(make_client):
    def handler(request):
        if "query/01g" in str(request.url):
            return json_response({"totalSize": 4000, "done": True, "records": []})
        return json_response(
            {
                "totalSize": 4000,
                "done": False,
                "records": [{"Id": "001"}],
                "nextRecordsUrl": "/services/data/v62.0/query/01g-2000",
            }
        )

    with make_client(handler) as client:
        first = json.loads(handle_run_soql_query(client, {"query": "SELECT Id FROM Account"}))
        assert first["next_url"] == "/services/data/v62.0/query/01g-2000"
        second = json.loads(
            handle_run_soql_query(client, {"next_url": first["next_url"]})
        )
    assert second["done"] is True


def test_soql_requires_query_or_next_url(make_client):
    with make_client(lambda request: json_response({})) as client:
        assert handle_run_soql_query(client, {}).startswith("ERROR:")


def test_soql_truncates_oversized_result_sets(make_client):
    big = {
        "totalSize": 2000,
        "done": True,
        "records": [
            {"Id": f"001{i:015d}", "Description": "x" * 100} for i in range(1000)
        ],
    }

    with make_client(lambda request: json_response(big)) as client:
        payload = json.loads(
            handle_run_soql_query(client, {"query": "SELECT Id FROM Account"})
        )
    assert payload["truncated"] is True
    assert len(payload["records"]) < 1000
    assert "LIMIT" in payload["note"]


def test_search_builds_parameterized_body(make_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return json_response(
            {
                "searchRecords": [
                    {
                        "attributes": {"type": "Account", "url": "/x"},
                        "Id": "001",
                        "Name": "Acme",
                    }
                ]
            }
        )

    with make_client(handler) as client:
        payload = json.loads(
            handle_search_records(
                client,
                {
                    "search_term": "Acme",
                    "object_types": ["Account", "Contact"],
                    "fields": ["Id", "Name"],
                    "limit": 5,
                },
            )
        )
    assert seen["path"].endswith("/parameterizedSearch/")
    assert seen["body"] == {
        "q": "Acme",
        "in": "ALL",
        "overallLimit": 5,
        "sobjects": [
            {"name": "Account", "fields": ["Id", "Name"]},
            {"name": "Contact", "fields": ["Id", "Name"]},
        ],
    }
    assert payload["count"] == 1
    assert payload["records"][0] == {"object_type": "Account", "Id": "001", "Name": "Acme"}


def test_search_validation(make_client):
    with make_client(lambda request: json_response({})) as client:
        assert handle_search_records(client, {"search_term": "x"}).startswith("ERROR:")
        assert handle_search_records(
            client, {"search_term": "Acme", "fields": ["Id"]}
        ).startswith("ERROR:")
