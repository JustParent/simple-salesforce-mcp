"""Metadata tools: describe, object list, org info."""

from __future__ import annotations

from mcp.types import Tool, ToolAnnotations

from ..formatting import to_compact_json, to_pretty_json
from ..sf_client import SalesforceApiError, SalesforceAuthError, SalesforceClient
from ._confirm import missing_params

MAX_OBJECT_LIST = 500
_SYSTEM_SUFFIXES = ("Share", "History", "Feed", "ChangeEvent", "Tag")

DESCRIBE_OBJECT = Tool(
    name="describe_object",
    description=(
        "Get a Salesforce object's fields: API names, types, required flags, and "
        "picklist values. Use before writing SOQL or creating/updating records."
    ),
    inputSchema={
        "type": "object",
        "required": ["object_type"],
        "properties": {
            "object_type": {
                "type": "string",
                "description": "Object API name, e.g. Account or My_Object__c.",
            },
            "detail": {
                "type": "string",
                "enum": ["basic", "full"],
                "description": (
                    "basic (default): fields with type/required/picklists. "
                    "full: adds child relationships, record types, per-field help "
                    "text and defaults."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Describe object",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def trim_describe(raw: dict, full: bool) -> dict:
    """Reduce a raw describe payload (often hundreds of KB) to what a model needs."""
    out = {
        "name": raw.get("name"),
        "label": raw.get("label"),
        "custom": raw.get("custom"),
        "key_prefix": raw.get("keyPrefix"),
        "createable": raw.get("createable"),
        "updateable": raw.get("updateable"),
        "deletable": raw.get("deletable"),
        "queryable": raw.get("queryable"),
        "searchable": raw.get("searchable"),
    }

    fields = []
    for f in raw.get("fields") or []:
        entry: dict = {
            "name": f.get("name"),
            "label": f.get("label"),
            "type": f.get("type"),
        }
        if f.get("createable") and not f.get("nillable") and not f.get("defaultedOnCreate"):
            entry["required"] = True
        if not f.get("createable") and not f.get("updateable"):
            entry["readonly"] = True
        if f.get("type") in ("string", "textarea", "phone", "url", "email") and f.get(
            "length"
        ):
            entry["length"] = f["length"]
        if f.get("type") in ("picklist", "multipicklist"):
            values = [
                v for v in f.get("picklistValues") or [] if v.get("active", True)
            ]
            entry["picklist_values"] = [
                v["value"]
                if v.get("label") in (None, v.get("value"))
                else {"value": v.get("value"), "label": v.get("label")}
                for v in values
            ]
        if f.get("referenceTo"):
            entry["reference_to"] = f["referenceTo"]
            if f.get("relationshipName"):
                entry["relationship_name"] = f["relationshipName"]
        if f.get("calculated"):
            entry["calculated"] = True
        if f.get("unique"):
            entry["unique"] = True
        if f.get("externalId"):
            entry["external_id"] = True
        if full:
            if f.get("inlineHelpText"):
                entry["help_text"] = f["inlineHelpText"]
            if f.get("defaultValue") is not None:
                entry["default_value"] = f["defaultValue"]
            if f.get("controllerName"):
                entry["controller_name"] = f["controllerName"]
        fields.append(entry)
    out["fields"] = fields

    if full:
        out["child_relationships"] = [
            {
                "child_object": rel.get("childSObject"),
                "relationship_name": rel.get("relationshipName"),
                "field": rel.get("field"),
            }
            for rel in raw.get("childRelationships") or []
            if rel.get("relationshipName")
        ]
        out["record_types"] = [
            {
                "name": rt.get("name"),
                "id": rt.get("recordTypeId"),
                "default": rt.get("defaultRecordTypeMapping"),
            }
            for rt in raw.get("recordTypeInfos") or []
        ]
    return out


def handle_describe_object(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "object_type")
    if err:
        return err
    detail = arguments.get("detail") or "basic"
    if detail not in ("basic", "full"):
        return "ERROR: detail must be 'basic' or 'full'."
    raw = client.describe(arguments["object_type"])
    return to_compact_json(trim_describe(raw, full=detail == "full"))


LIST_OBJECTS = Tool(
    name="list_objects",
    description=(
        "List the org's Salesforce objects (API name and label). System noise "
        "(Share/History/Feed/ChangeEvent tables, non-queryable objects) is excluded "
        "by default."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Case-insensitive substring match on API name or label.",
            },
            "include_system": {
                "type": "boolean",
                "description": "Include non-queryable and system objects (default false).",
            },
        },
    },
    annotations=ToolAnnotations(
        title="List objects",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_list_objects(client: SalesforceClient, arguments: dict) -> str:
    raw = client.list_sobjects()
    filter_term = str(arguments.get("filter") or "").lower()
    include_system = bool(arguments.get("include_system"))

    objects = []
    for sobject in raw.get("sobjects") or []:
        name = sobject.get("name") or ""
        label = sobject.get("label") or ""
        custom = bool(sobject.get("custom"))
        if not include_system:
            if not sobject.get("queryable"):
                continue
            if not custom and name.endswith(_SYSTEM_SUFFIXES):
                continue
        if filter_term and filter_term not in name.lower() and filter_term not in label.lower():
            continue
        entry = {"name": name, "label": label}
        if custom:
            entry["custom"] = True
        objects.append(entry)

    payload: dict = {"count": len(objects), "objects": objects[:MAX_OBJECT_LIST]}
    if len(objects) > MAX_OBJECT_LIST:
        payload["note"] = (
            f"Showing {MAX_OBJECT_LIST} of {len(objects)} objects. "
            "Narrow with the filter parameter."
        )
    return to_compact_json(payload)


GET_ORG_INFO = Tool(
    name="get_org_info",
    description=(
        "Current Salesforce user, org id/name/type, instance URL, and API version "
        "in use. Call this first to establish context."
    ),
    inputSchema={"type": "object", "properties": {}},
    annotations=ToolAnnotations(
        title="Get org info",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_get_org_info(client: SalesforceClient, arguments: dict) -> str:
    creds = client.creds
    payload: dict = {
        "instance_url": creds.instance_url,
        "api_version": client.api_version,
        "auth_source": (
            "environment variables" if creds.source == "env" else "Salesforce CLI auth store"
        ),
    }
    if creds.username:
        payload["username"] = creds.username
    if creds.org_id:
        payload["org_id"] = creds.org_id

    try:
        result = client.query(
            "SELECT Id, Name, OrganizationType, IsSandbox, InstanceName FROM Organization"
        )
    except SalesforceAuthError:
        raise
    except SalesforceApiError as exc:
        payload["organization_error"] = exc.for_model()
    else:
        records = result.get("records") or []
        if records:
            org = records[0]
            payload["organization"] = {
                "id": org.get("Id"),
                "name": org.get("Name"),
                "type": org.get("OrganizationType"),
                "is_sandbox": org.get("IsSandbox"),
                "instance": org.get("InstanceName"),
            }

    if "username" not in payload:
        info = client.userinfo()
        if info:
            username = info.get("preferred_username") or info.get("email")
            if username:
                payload["username"] = username
            if info.get("organization_id"):
                payload.setdefault("org_id", info["organization_id"])
    return to_pretty_json(payload)


TOOLS = [
    (DESCRIBE_OBJECT, handle_describe_object),
    (LIST_OBJECTS, handle_list_objects),
    (GET_ORG_INFO, handle_get_org_info),
]
