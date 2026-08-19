import json

from conftest import TEST_INSTANCE, json_response

from simple_salesforce_mcp.tools.metadata import (
    handle_describe_object,
    handle_get_org_info,
    handle_list_objects,
    trim_describe,
)

DESCRIBE_FIXTURE = {
    "name": "Account",
    "label": "Account",
    "custom": False,
    "keyPrefix": "001",
    "createable": True,
    "updateable": True,
    "deletable": True,
    "queryable": True,
    "searchable": True,
    "urls": {"sobject": "/services/data/v62.0/sobjects/Account"},
    "actionOverrides": [],
    "fields": [
        {
            "name": "Id",
            "label": "Account ID",
            "type": "id",
            "createable": False,
            "updateable": False,
            "nillable": False,
            "defaultedOnCreate": True,
            "soapType": "tns:ID",
        },
        {
            "name": "Name",
            "label": "Account Name",
            "type": "string",
            "createable": True,
            "updateable": True,
            "nillable": False,
            "defaultedOnCreate": False,
            "length": 255,
            "soapType": "xsd:string",
        },
        {
            "name": "Industry",
            "label": "Industry",
            "type": "picklist",
            "createable": True,
            "updateable": True,
            "nillable": True,
            "defaultedOnCreate": False,
            "picklistValues": [
                {"value": "Energy", "label": "Energy", "active": True},
                {"value": "Retail", "label": "Retail Sector", "active": True},
                {"value": "Old", "label": "Old", "active": False},
            ],
        },
        {
            "name": "OwnerId",
            "label": "Owner ID",
            "type": "reference",
            "createable": True,
            "updateable": True,
            "nillable": False,
            "defaultedOnCreate": True,
            "referenceTo": ["User"],
            "relationshipName": "Owner",
        },
        {
            "name": "Score__c",
            "label": "Score",
            "type": "double",
            "createable": False,
            "updateable": False,
            "nillable": True,
            "defaultedOnCreate": False,
            "calculated": True,
            "inlineHelpText": "Computed score",
        },
    ],
    "childRelationships": [
        {"childSObject": "Contact", "relationshipName": "Contacts", "field": "AccountId"},
        {"childSObject": "AccountFeed", "relationshipName": None, "field": "ParentId"},
    ],
    "recordTypeInfos": [
        {
            "name": "Master",
            "recordTypeId": "012000000000000AAA",
            "defaultRecordTypeMapping": True,
        }
    ],
}


def test_trim_describe_basic():
    out = trim_describe(DESCRIBE_FIXTURE, full=False)
    assert out["name"] == "Account"
    assert "urls" not in out
    assert "child_relationships" not in out
    by_name = {f["name"]: f for f in out["fields"]}

    assert by_name["Name"]["required"] is True
    assert by_name["Name"]["length"] == 255
    assert "required" not in by_name["Industry"]
    assert by_name["Industry"]["picklist_values"] == [
        "Energy",
        {"value": "Retail", "label": "Retail Sector"},
    ]
    # defaultedOnCreate fields (Id, OwnerId) are not "required"
    assert "required" not in by_name["Id"]
    assert by_name["Id"]["readonly"] is True
    assert by_name["OwnerId"]["reference_to"] == ["User"]
    assert by_name["OwnerId"]["relationship_name"] == "Owner"
    assert by_name["Score__c"]["calculated"] is True
    assert "help_text" not in by_name["Score__c"]
    assert "soapType" not in json.dumps(out)


def test_trim_describe_full():
    out = trim_describe(DESCRIBE_FIXTURE, full=True)
    assert out["child_relationships"] == [
        {"child_object": "Contact", "relationship_name": "Contacts", "field": "AccountId"}
    ]
    assert out["record_types"] == [{"name": "Master", "id": "012000000000000AAA", "default": True}]
    by_name = {f["name"]: f for f in out["fields"]}
    assert by_name["Score__c"]["help_text"] == "Computed score"


def test_trim_describe_shrinks_payload():
    raw = json.dumps(DESCRIBE_FIXTURE)
    trimmed = json.dumps(trim_describe(DESCRIBE_FIXTURE, full=False))
    assert len(trimmed) < len(raw)


def test_describe_object_tool(make_client):
    with make_client(lambda request: json_response(DESCRIBE_FIXTURE)) as client:
        payload = json.loads(handle_describe_object(client, {"object_type": "Account"}))
    assert payload["name"] == "Account"
    assert "child_relationships" not in payload


SOBJECTS_FIXTURE = {
    "sobjects": [
        {"name": "Account", "label": "Account", "custom": False, "queryable": True},
        {"name": "AccountShare", "label": "Account Share", "custom": False, "queryable": True},
        {"name": "AccountHistory", "label": "Account History", "custom": False, "queryable": True},
        {
            "name": "AccountChangeEvent",
            "label": "Change Event",
            "custom": False,
            "queryable": False,
        },
        {"name": "Contact", "label": "Contact", "custom": False, "queryable": True},
        {"name": "Foo__c", "label": "Foo", "custom": True, "queryable": True},
        {"name": "Hidden", "label": "Hidden", "custom": False, "queryable": False},
    ]
}


def test_list_objects_default_filters_noise(make_client):
    with make_client(lambda request: json_response(SOBJECTS_FIXTURE)) as client:
        payload = json.loads(handle_list_objects(client, {}))
    names = [o["name"] for o in payload["objects"]]
    assert names == ["Account", "Contact", "Foo__c"]
    assert payload["count"] == 3


def test_list_objects_include_system(make_client):
    with make_client(lambda request: json_response(SOBJECTS_FIXTURE)) as client:
        payload = json.loads(handle_list_objects(client, {"include_system": True}))
    assert payload["count"] == len(SOBJECTS_FIXTURE["sobjects"])


def test_list_objects_filter(make_client):
    with make_client(lambda request: json_response(SOBJECTS_FIXTURE)) as client:
        payload = json.loads(handle_list_objects(client, {"filter": "foo"}))
    assert [o["name"] for o in payload["objects"]] == ["Foo__c"]
    assert payload["objects"][0]["custom"] is True


ORG_RECORD = {
    "attributes": {"type": "Organization"},
    "Id": "00D000000000001EAA",
    "Name": "Acme Corp",
    "OrganizationType": "Enterprise Edition",
    "IsSandbox": False,
    "InstanceName": "EU45",
}


def test_get_org_info(make_client):
    import httpx

    def handler(request):
        if "query" in request.url.path:
            return json_response({"totalSize": 1, "done": True, "records": [ORG_RECORD]})
        return httpx.Response(403, text="")

    with make_client(handler) as client:
        payload = json.loads(handle_get_org_info(client, {}))
    assert payload["organization"]["name"] == "Acme Corp"
    assert payload["username"] == "user@example.org"
    assert payload["instance_url"] == TEST_INSTANCE
    assert payload["auth_source"] == "Salesforce CLI auth store"


def test_get_org_info_env_mode_uses_userinfo(clean_env, monkeypatch):
    import httpx

    from simple_salesforce_mcp.auth import resolve_credentials
    from simple_salesforce_mcp.sf_client import SalesforceClient

    monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "envtoken")
    monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "https://env.my.salesforce.com")

    def handler(request):
        if "userinfo" in request.url.path:
            return json_response(
                {"preferred_username": "env@example.org", "organization_id": "00Dxx"}
            )
        return json_response({"totalSize": 1, "done": True, "records": [ORG_RECORD]})

    client = SalesforceClient(resolve_credentials(), transport=httpx.MockTransport(handler))
    with client:
        payload = json.loads(handle_get_org_info(client, {}))
    assert payload["username"] == "env@example.org"
    assert payload["org_id"] == "00Dxx"
    assert payload["auth_source"] == "environment variables"
