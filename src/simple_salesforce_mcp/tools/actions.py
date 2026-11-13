"""Invocable action tools: list, describe, and invoke (confirm-gated).

These wrap Salesforce's Actions REST API (``/actions/standard`` and
``/actions/custom/{type}``), the same actions Flow Builder offers: standard
actions such as the Knowledge lifecycle (createDraftFromOnlineKnowledgeArticle,
publishKnowledgeArticles, archiveKnowledgeArticles), submit for approval or
send email, plus the org's own autolaunched Flows, invocable Apex, quick actions
and email alerts. What an action does is opaque from here, so every invoke is
confirm-gated.
"""

from __future__ import annotations

from mcp.types import Tool, ToolAnnotations

from ..formatting import to_compact_json
from ..sf_client import SalesforceApiError, SalesforceClient
from ._confirm import check_confirm, missing_params

MAX_ACTION_LIST = 500
MAX_INPUTS = 200

_NOT_FOUND_HINT = (
    "Check the action name and action_type with list_actions; names are case-sensitive, "
    "and custom actions live under their type (flow, apex, quickAction, ...)."
)

_ACTION_TYPE_SCHEMA = {
    "type": "string",
    "description": (
        "standard for Salesforce's built-in actions, or a custom action type: flow "
        "(autolaunched Flows), apex (@InvocableMethod classes), quickAction, "
        "emailAlert, ... list_actions with action_type=custom shows the types this "
        "org has."
    ),
}
_ACTION_NAME_SCHEMA = {
    "type": "string",
    "description": (
        "Action API name from list_actions, e.g. publishKnowledgeArticles, My_Flow, "
        "or Case.LogACall for an object-scoped quick action."
    ),
}

LIST_ACTIONS = Tool(
    name="list_actions",
    description=(
        "List invocable actions in the org: standard actions (Knowledge publish / "
        "archive / edit as draft, submit for approval, send email, Chatter post, ...) "
        "or custom ones (autolaunched Flows, invocable Apex, quick actions, email "
        "alerts). Use when a change is an operation rather than a field edit, e.g. a "
        "read-only status field. Then describe_action, then invoke_action."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "action_type": {
                **_ACTION_TYPE_SCHEMA,
                "description": (
                    "standard (default) lists built-in actions; custom lists the custom "
                    "action types this org has; a type such as flow or apex lists the "
                    "actions of that type."
                ),
            },
            "filter": {
                "type": "string",
                "description": (
                    "Case-insensitive substring matched against action name and label, "
                    "e.g. knowledge."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="List actions",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_list_actions(client: SalesforceClient, arguments: dict) -> str:
    action_type = arguments.get("action_type") or "standard"
    raw = client.list_actions(action_type)
    if action_type == "custom":
        types = sorted(raw) if isinstance(raw, dict) else []
        return to_compact_json(
            {
                "custom_action_types": types,
                "next": "call list_actions with one of these as action_type",
            }
        )

    needle = (arguments.get("filter") or "").lower()
    actions = []
    for entry in raw.get("actions") or []:
        name = entry.get("name") or ""
        label = entry.get("label") or ""
        if needle and needle not in name.lower() and needle not in label.lower():
            continue
        item = {"name": name, "label": label}
        if entry.get("type"):
            item["type"] = entry["type"]
        actions.append(item)
    actions.sort(key=lambda a: a["name"].lower())

    out: dict = {"action_type": action_type, "count": len(actions)}
    if len(actions) > MAX_ACTION_LIST:
        out["truncated"] = f"showing the first {MAX_ACTION_LIST}; pass filter to narrow"
        actions = actions[:MAX_ACTION_LIST]
    out["actions"] = actions
    return to_compact_json(out)


DESCRIBE_ACTION = Tool(
    name="describe_action",
    description=(
        "Get an invocable action's inputs (names, types, required flags, allowed "
        "values) and outputs. Use before invoke_action."
    ),
    inputSchema={
        "type": "object",
        "required": ["action_type", "name"],
        "properties": {
            "action_type": _ACTION_TYPE_SCHEMA,
            "name": _ACTION_NAME_SCHEMA,
        },
    },
    annotations=ToolAnnotations(
        title="Describe action",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def _trim_param(p: dict) -> dict:
    entry: dict = {"name": p.get("name"), "type": p.get("type")}
    if p.get("required"):
        entry["required"] = True
    if p.get("label") and p.get("label") != p.get("name"):
        entry["label"] = p["label"]
    if p.get("description"):
        entry["description"] = p["description"]
    if p.get("sobjectType"):
        entry["sobject_type"] = p["sobjectType"]
    max_occurs = p.get("maxOccurs")
    if isinstance(max_occurs, int) and max_occurs > 1:
        entry["max_occurs"] = max_occurs
    picklist = p.get("picklistValues") or []
    if picklist:
        entry["allowed_values"] = [v.get("value") if isinstance(v, dict) else v for v in picklist]
    return entry


def handle_describe_action(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "action_type", "name")
    if err:
        return err
    raw = _with_not_found_hint(
        lambda: client.describe_action(arguments["action_type"], arguments["name"])
    )
    out: dict = {"name": raw.get("name"), "label": raw.get("label")}
    for key in ("description", "type", "category", "targetEntityName"):
        if raw.get(key):
            out[key] = raw[key]
    out["inputs"] = [_trim_param(p) for p in raw.get("inputs") or []]
    out["outputs"] = [_trim_param(p) for p in raw.get("outputs") or []]
    return to_compact_json(out)


INVOKE_ACTION = Tool(
    name="invoke_action",
    description=(
        "Run an invocable action (standard, Flow, Apex, quick action, ...) for one or "
        "more inputs. Its effect depends on the action and may be irreversible, so it "
        "requires confirm=true: first present the action and every input value to the "
        "user and get their approval. Call describe_action first for the input names."
    ),
    inputSchema={
        "type": "object",
        "required": ["action_type", "name", "inputs", "confirm"],
        "properties": {
            "action_type": _ACTION_TYPE_SCHEMA,
            "name": _ACTION_NAME_SCHEMA,
            "inputs": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "One object per invocation, mapping input names from "
                    "describe_action to values, e.g. "
                    '[{"contextId": "500...", "Subject": "Called back"}]. '
                    "Use [{}] for an action that takes no inputs."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be literally true. Before setting it, show the user the action "
                    "and every input value and get their approval."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Invoke action",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)


def handle_invoke_action(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "action_type", "name")
    if err:
        return err
    inputs = arguments.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(i, dict) for i in inputs):
        return (
            "ERROR: inputs must be a non-empty array of objects (use [{}] for an action "
            "with no inputs)."
        )
    if len(inputs) > MAX_INPUTS:
        return f"ERROR: at most {MAX_INPUTS} inputs per call; split the batch."
    action_type = arguments["action_type"]
    name = arguments["name"]
    gate = check_confirm(
        arguments,
        f"run the {action_type} action {name} with inputs {to_compact_json(inputs)}",
        present="the action name and every input value",
    )
    if gate:
        return gate

    raw = _with_not_found_hint(lambda: client.invoke_action(action_type, name, inputs))
    results = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        entry: dict = {"input": index, "success": bool(item.get("isSuccess"))}
        if item.get("outputValues"):
            entry["outputs"] = item["outputValues"]
        errors = item.get("errors") or []
        if errors:
            entry["errors"] = [
                {
                    k: v
                    for k, v in {
                        "code": e.get("statusCode"),
                        "message": e.get("message"),
                        "fields": e.get("fields") or None,
                    }.items()
                    if v
                }
                for e in errors
            ]
        results.append(entry)

    succeeded = sum(1 for r in results if r["success"])
    total = len(results)
    if total and succeeded == 0:
        head = (
            f"ERROR: action {name} reported failure for all {total} input(s). The "
            "reason is in errors, or per item in outputs (items marked Success did go "
            "through)."
        )
    elif succeeded < total:
        head = (
            f"PARTIAL: action {name} succeeded for {succeeded} of {total} inputs; check "
            "errors and outputs on the failed ones."
        )
    else:
        head = f"Ran action {name}: {succeeded} of {total} succeeded."
    return f"{head}\n{to_compact_json(results)}"


def _with_not_found_hint(call):
    try:
        return call()
    except SalesforceApiError as exc:
        if exc.status == 404 or exc.error_code == "NOT_FOUND":
            exc.hint = _NOT_FOUND_HINT
        raise


TOOLS = [
    (LIST_ACTIONS, handle_list_actions),
    (DESCRIBE_ACTION, handle_describe_action),
    (INVOKE_ACTION, handle_invoke_action),
]
