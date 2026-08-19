"""Single-record CRUD tools. Update and delete are confirm-gated."""

from __future__ import annotations

from mcp.types import Tool, ToolAnnotations

from ..formatting import strip_attributes, to_compact_json
from ..sf_client import SalesforceClient
from ._confirm import check_confirm, missing_params

_OBJECT_TYPE_SCHEMA = {
    "type": "string",
    "description": "Object API name, e.g. Account, Contact, or My_Object__c.",
}
_RECORD_ID_SCHEMA = {
    "type": "string",
    "description": "15- or 18-character Salesforce record Id.",
}

GET_RECORD = Tool(
    name="get_record",
    description="Fetch a single Salesforce record by Id.",
    inputSchema={
        "type": "object",
        "required": ["object_type", "record_id"],
        "properties": {
            "object_type": _OBJECT_TYPE_SCHEMA,
            "record_id": _RECORD_ID_SCHEMA,
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Field API names to return (relationship paths like Account.Name "
                    "allowed). Omit for all accessible fields."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Get record",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_get_record(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "object_type", "record_id")
    if err:
        return err
    result = client.get_sobject(
        arguments["object_type"], arguments["record_id"], fields=arguments.get("fields")
    )
    return to_compact_json(strip_attributes(result))


CREATE_RECORD = Tool(
    name="create_record",
    description=(
        "Create a new Salesforce record. Use describe_object first to find the "
        "required fields and valid picklist values."
    ),
    inputSchema={
        "type": "object",
        "required": ["object_type", "data"],
        "properties": {
            "object_type": _OBJECT_TYPE_SCHEMA,
            "data": {
                "type": "object",
                "description": (
                    'Field API names to values, e.g. {"Name": "Acme", "Industry": "Energy"}.'
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Create record",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)


def handle_create_record(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "object_type", "data")
    if err:
        return err
    data = arguments["data"]
    if not isinstance(data, dict) or not data:
        return "ERROR: data must be a non-empty object of field API names to values."
    result = client.create(arguments["object_type"], data)
    text = f"Created {arguments['object_type']} {result.get('id')}."
    warnings = result.get("warnings")
    if warnings:
        text += f" Warnings: {to_compact_json(warnings)}"
    return text


_CONFIRM_SCHEMA = {
    "type": "boolean",
    "description": (
        "Must be literally true. Before setting it, show the user the exact record "
        "and field changes and get their approval."
    ),
}

UPDATE_RECORD = Tool(
    name="update_record",
    description=(
        "Update fields on an existing Salesforce record. Requires confirm=true: "
        "first present the exact change to the user and get their approval."
    ),
    inputSchema={
        "type": "object",
        "required": ["object_type", "record_id", "data", "confirm"],
        "properties": {
            "object_type": _OBJECT_TYPE_SCHEMA,
            "record_id": _RECORD_ID_SCHEMA,
            "data": {
                "type": "object",
                "description": "Only the fields to change, as field API names to values.",
            },
            "confirm": _CONFIRM_SCHEMA,
        },
    },
    annotations=ToolAnnotations(
        title="Update record",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_update_record(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "object_type", "record_id", "data")
    if err:
        return err
    raw = arguments["data"]
    if not isinstance(raw, dict):
        return "ERROR: data must be an object of field API names to values."
    # Salesforce rejects a PATCH body containing Id or attributes.
    data = {k: v for k, v in raw.items() if k not in ("Id", "id", "attributes")}
    if not data:
        return "ERROR: data must contain at least one field to change."
    object_type = arguments["object_type"]
    record_id = arguments["record_id"]
    gate = check_confirm(
        arguments, f"update {object_type} {record_id} setting {to_compact_json(data)}"
    )
    if gate:
        return gate
    client.update(object_type, record_id, data)
    return f"Updated {object_type} {record_id}: set {to_compact_json(data)}."


DELETE_RECORD = Tool(
    name="delete_record",
    description=(
        "Permanently delete a Salesforce record. Requires confirm=true: first "
        "present the deletion to the user and get their approval."
    ),
    inputSchema={
        "type": "object",
        "required": ["object_type", "record_id", "confirm"],
        "properties": {
            "object_type": _OBJECT_TYPE_SCHEMA,
            "record_id": _RECORD_ID_SCHEMA,
            "confirm": _CONFIRM_SCHEMA,
        },
    },
    annotations=ToolAnnotations(
        title="Delete record",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_delete_record(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "object_type", "record_id")
    if err:
        return err
    object_type = arguments["object_type"]
    record_id = arguments["record_id"]
    gate = check_confirm(arguments, f"permanently delete {object_type} {record_id}")
    if gate:
        return gate
    client.delete(object_type, record_id)
    return f"Deleted {object_type} {record_id}."


TOOLS = [
    (GET_RECORD, handle_get_record),
    (CREATE_RECORD, handle_create_record),
    (UPDATE_RECORD, handle_update_record),
    (DELETE_RECORD, handle_delete_record),
]
