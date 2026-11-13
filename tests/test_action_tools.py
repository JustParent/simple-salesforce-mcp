import json

import pytest
from conftest import json_response

from simple_salesforce_mcp.sf_client import SalesforceApiError
from simple_salesforce_mcp.tools.actions import (
    MAX_INPUTS,
    handle_describe_action,
    handle_invoke_action,
    handle_list_actions,
)

KNOWLEDGE_ACTION = "createDraftFromOnlineKnowledgeArticle"


@pytest.fixture
def refusing_client(make_client):
    """A client whose transport fails the test if any HTTP request is made."""

    def handler(request):
        pytest.fail(f"unexpected HTTP call: {request.method} {request.url}")

    return make_client(handler)


def test_list_standard_actions_filters_and_sorts(make_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return json_response(
            {
                "actions": [
                    {
                        "name": "publishKnowledgeArticles",
                        "label": "Publish Knowledge Articles",
                        "type": "PUBLISH_KNOWLEDGE_ARTICLES",
                        "url": "/x",
                    },
                    {"name": "emailSimple", "label": "Send Email", "type": "EMAIL_SIMPLE"},
                    {"name": KNOWLEDGE_ACTION, "label": "Create Draft from Online Article"},
                ]
            }
        )

    with make_client(handler) as client:
        payload = json.loads(handle_list_actions(client, {"filter": "KNOWLEDGE"}))
    assert seen["path"] == "/services/data/v62.0/actions/standard"
    assert payload["action_type"] == "standard"
    assert payload["count"] == 2
    assert [a["name"] for a in payload["actions"]] == [
        KNOWLEDGE_ACTION,
        "publishKnowledgeArticles",
    ]
    assert "url" not in payload["actions"][1]


def test_list_custom_returns_types(make_client):
    def handler(request):
        assert request.url.path.endswith("/actions/custom")
        return json_response(
            {
                "flow": "/services/data/v62.0/actions/custom/flow",
                "apex": "/services/data/v62.0/actions/custom/apex",
            }
        )

    with make_client(handler) as client:
        payload = json.loads(handle_list_actions(client, {"action_type": "custom"}))
    assert payload["custom_action_types"] == ["apex", "flow"]


def test_list_custom_type(make_client):
    def handler(request):
        assert request.url.path.endswith("/actions/custom/flow")
        return json_response({"actions": [{"name": "Escalate_Case", "label": "Escalate"}]})

    with make_client(handler) as client:
        payload = json.loads(handle_list_actions(client, {"action_type": "flow"}))
    assert payload["actions"] == [{"name": "Escalate_Case", "label": "Escalate"}]


def test_list_rejects_bad_action_type(refusing_client):
    with refusing_client as client, pytest.raises(SalesforceApiError):
        handle_list_actions(client, {"action_type": "../sobjects"})


def test_describe_action_trims_params(make_client):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path.endswith(f"/actions/standard/{KNOWLEDGE_ACTION}")
        return json_response(
            {
                "name": KNOWLEDGE_ACTION,
                "label": "Create Draft from Online Article",
                "type": "CREATE_DRAFT_FROM_ONLINE_KNOWLEDGE_ARTICLE",
                "description": None,
                "inputs": [
                    {
                        "name": "action",
                        "label": "Action",
                        "type": "STRING",
                        "required": True,
                        "description": "Edit action",
                        "maxOccurs": 1,
                        "byteLength": 0,
                        "picklistValues": [
                            {"value": "EDIT_AS_DRAFT_ARTICLE", "label": "x"},
                            {"value": "EDIT_AS_DRAFT_TRANSLATION", "label": "y"},
                        ],
                    },
                    {
                        "name": "unpublish",
                        "label": "unpublish",
                        "type": "BOOLEAN",
                        "required": False,
                        "picklistValues": None,
                    },
                    {
                        "name": "articleIds",
                        "label": "Article IDs",
                        "type": "ID",
                        "required": False,
                        "maxOccurs": 100,
                        "sobjectType": "KnowledgeArticle",
                    },
                ],
                "outputs": [{"name": "result", "type": "STRING"}],
            }
        )

    with make_client(handler) as client:
        payload = json.loads(
            handle_describe_action(client, {"action_type": "standard", "name": KNOWLEDGE_ACTION})
        )
    assert "description" not in payload
    assert payload["inputs"] == [
        {
            "name": "action",
            "type": "STRING",
            "required": True,
            "label": "Action",
            "description": "Edit action",
            "allowed_values": ["EDIT_AS_DRAFT_ARTICLE", "EDIT_AS_DRAFT_TRANSLATION"],
        },
        {"name": "unpublish", "type": "BOOLEAN"},
        {
            "name": "articleIds",
            "type": "ID",
            "label": "Article IDs",
            "sobject_type": "KnowledgeArticle",
            "max_occurs": 100,
        },
    ]
    assert payload["outputs"] == [{"name": "result", "type": "STRING"}]


def test_describe_quick_action_uses_object_segment(make_client):
    def handler(request):
        assert request.url.path.endswith("/actions/custom/quickAction/Case/LogACall")
        return json_response({"name": "Case.LogACall", "inputs": [], "outputs": []})

    with make_client(handler) as client:
        handle_describe_action(client, {"action_type": "quickAction", "name": "Case.LogACall"})


def test_describe_not_found_gets_action_hint(make_client):
    def handler(request):
        return json_response(
            [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}],
            status=404,
        )

    with make_client(handler) as client, pytest.raises(SalesforceApiError) as info:
        handle_describe_action(client, {"action_type": "flow", "name": "Nope"})
    assert "list_actions" in info.value.for_model()


@pytest.mark.parametrize("name", ["a/b", "a.b.c", "../x", "", "a b"])
def test_rejects_unsafe_action_names(refusing_client, name):
    with refusing_client as client:
        if not name:
            assert handle_describe_action(
                client, {"action_type": "standard", "name": name}
            ).startswith("ERROR: missing")
        else:
            with pytest.raises(SalesforceApiError):
                handle_describe_action(client, {"action_type": "standard", "name": name})


def test_custom_type_cannot_be_invoked_directly(refusing_client):
    with refusing_client as client, pytest.raises(SalesforceApiError):
        handle_invoke_action(
            client,
            {"action_type": "custom", "name": "My_Flow", "inputs": [{}], "confirm": True},
        )


_INVOKE_ARGS = {
    "action_type": "standard",
    "name": KNOWLEDGE_ACTION,
    "inputs": [{"action": "EDIT_AS_DRAFT_ARTICLE", "unpublish": False}],
}


def test_invoke_without_confirm_is_blocked(refusing_client):
    with refusing_client as client:
        text = handle_invoke_action(client, dict(_INVOKE_ARGS))
    assert text.startswith("CONFIRMATION REQUIRED")
    assert "no changes were made" in text
    assert KNOWLEDGE_ACTION in text
    assert '"action":"EDIT_AS_DRAFT_ARTICLE"' in text
    assert "every input value" in text


def test_invoke_with_string_true_is_still_blocked(refusing_client):
    with refusing_client as client:
        text = handle_invoke_action(client, {**_INVOKE_ARGS, "confirm": "true"})
    assert text.startswith("CONFIRMATION REQUIRED")


@pytest.mark.parametrize("inputs", [None, [], {}, [1], ["x"]])
def test_invoke_validates_inputs(refusing_client, inputs):
    with refusing_client as client:
        text = handle_invoke_action(client, {**_INVOKE_ARGS, "inputs": inputs, "confirm": True})
    assert text.startswith("ERROR: inputs must be")


def test_invoke_caps_inputs(refusing_client):
    with refusing_client as client:
        text = handle_invoke_action(
            client, {**_INVOKE_ARGS, "inputs": [{}] * (MAX_INPUTS + 1), "confirm": True}
        )
    assert text.startswith("ERROR: at most")


def test_invoke_posts_inputs_and_reports_success(make_client):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return json_response(
            [
                {
                    "actionName": KNOWLEDGE_ACTION,
                    "errors": None,
                    "isSuccess": True,
                    "outputValues": {"kA05a000000h3L5CAI": "Success"},
                }
            ]
        )

    with make_client(handler) as client:
        text = handle_invoke_action(client, {**_INVOKE_ARGS, "confirm": True})
    assert seen["method"] == "POST"
    assert seen["path"].endswith(f"/actions/standard/{KNOWLEDGE_ACTION}")
    assert seen["body"] == {"inputs": _INVOKE_ARGS["inputs"]}
    head, body = text.split("\n", 1)
    assert head == f"Ran action {KNOWLEDGE_ACTION}: 1 of 1 succeeded."
    assert json.loads(body) == [
        {"input": 0, "success": True, "outputs": {"kA05a000000h3L5CAI": "Success"}}
    ]


def test_invoke_reports_partial_failure(make_client):
    def handler(request):
        return json_response(
            [
                {"isSuccess": True, "outputValues": None, "errors": None},
                {
                    "isSuccess": False,
                    "outputValues": None,
                    "errors": [{"statusCode": "INVALID_INPUT", "message": "bad id", "fields": []}],
                },
            ]
        )

    with make_client(handler) as client:
        text = handle_invoke_action(
            client, {**_INVOKE_ARGS, "inputs": [{"a": 1}, {"a": 2}], "confirm": True}
        )
    head, body = text.split("\n", 1)
    assert head.startswith("PARTIAL: action")
    assert "1 of 2" in head
    assert json.loads(body)[1] == {
        "input": 1,
        "success": False,
        "errors": [{"code": "INVALID_INPUT", "message": "bad id"}],
    }


def test_invoke_reports_total_failure_as_error(make_client):
    def handler(request):
        return json_response(
            [{"isSuccess": False, "errors": [{"statusCode": "X", "message": "no"}]}]
        )

    with make_client(handler) as client:
        text = handle_invoke_action(client, {**_INVOKE_ARGS, "confirm": True})
    assert text.startswith(f"ERROR: action {KNOWLEDGE_ACTION} failed for all 1 input(s)")
