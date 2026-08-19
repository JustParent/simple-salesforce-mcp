"""Query tools: raw SOQL and cross-object search."""

from __future__ import annotations

from mcp.types import Tool, ToolAnnotations

from ..formatting import cap_records, strip_attributes, to_compact_json
from ..sf_client import SalesforceClient
from ._confirm import missing_params

RUN_SOQL_QUERY = Tool(
    name="run_soql_query",
    description=(
        "Run a raw SOQL query against the Salesforce org. Read-only — SOQL cannot "
        "modify data. Use this as the escape hatch for anything the other tools "
        "don't cover: filtering, joins via relationship fields, aggregates, ORDER BY. "
        "Always include a LIMIT for exploratory queries. If a field or object name "
        "errors, check the exact API names with describe_object."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "SOQL query, e.g. SELECT Id, Name FROM Account "
                    "WHERE Industry = 'Energy' ORDER BY Name LIMIT 50"
                ),
            },
            "next_url": {
                "type": "string",
                "description": (
                    "Pagination cursor: pass the next_url value returned by a previous "
                    "call to fetch the next page. When set, query is ignored."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Run SOQL query",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_run_soql_query(client: SalesforceClient, arguments: dict) -> str:
    query = arguments.get("query")
    next_url = arguments.get("next_url")
    if next_url:
        result = client.query_next(next_url)
    elif query:
        result = client.query(query)
    else:
        return "ERROR: provide either query (SOQL) or next_url (from a previous page)."

    records = [strip_attributes(r) for r in result.get("records") or []]
    kept, dropped = cap_records(records)
    payload = {
        "total_size": result.get("totalSize"),
        "done": result.get("done"),
        "records": kept,
    }
    if result.get("nextRecordsUrl"):
        payload["next_url"] = result["nextRecordsUrl"]
    if dropped:
        payload["truncated"] = True
        note = (
            f"Response truncated: showing {len(kept)} of {len(records)} fetched "
            "records. Add a LIMIT or select fewer fields"
        )
        payload["note"] = note + (
            ", or pass next_url to page." if payload.get("next_url") else "."
        )
    return to_compact_json(payload)


SEARCH_RECORDS = Tool(
    name="search_records",
    description=(
        "Full-text search across Salesforce records (name, email, phone, and other "
        "searchable fields). Good for 'find the record for X' when you don't know "
        "the object or Id. For precise filtering use run_soql_query instead."
    ),
    inputSchema={
        "type": "object",
        "required": ["search_term"],
        "properties": {
            "search_term": {
                "type": "string",
                "description": "Text to search for (minimum 2 characters).",
            },
            "object_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Restrict to these objects, e.g. ["Account", "Contact"]. '
                    "Omit to search all searchable objects."
                ),
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Fields to return for each matched record (default: Id only). "
                    'E.g. ["Id", "Name"] — note not every object has Name (Case uses '
                    "CaseNumber). Requires object_types, and each field must exist on "
                    "every listed object."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum records overall (default 20, max 200).",
            },
        },
    },
    annotations=ToolAnnotations(
        title="Search records",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def handle_search_records(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "search_term")
    if err:
        return err
    term = str(arguments["search_term"]).strip()
    if len(term) < 2:
        return "ERROR: search_term must be at least 2 characters."

    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        return "ERROR: limit must be an integer."
    limit = max(1, min(limit, 200))

    object_types = arguments.get("object_types") or []
    fields = arguments.get("fields") or []
    if fields and not object_types:
        return "ERROR: fields requires object_types to be set."

    body: dict = {"q": term, "in": "ALL", "overallLimit": limit}
    if object_types:
        body["sobjects"] = [
            {"name": obj, **({"fields": fields} if fields else {})}
            for obj in object_types
        ]
    result = client.parameterized_search(body)

    records = []
    for rec in result.get("searchRecords") or []:
        attrs = rec.get("attributes") or {}
        flat = {"object_type": attrs.get("type")}
        flat.update({k: v for k, v in rec.items() if k != "attributes"})
        records.append(strip_attributes(flat))
    kept, dropped = cap_records(records)
    payload: dict = {"count": len(records), "records": kept}
    if dropped:
        payload["truncated"] = True
        payload["note"] = (
            f"Response truncated: showing {len(kept)} of {len(records)} matches. "
            "Lower the limit or request fewer fields."
        )
    return to_compact_json(payload)


TOOLS = [
    (RUN_SOQL_QUERY, handle_run_soql_query),
    (SEARCH_RECORDS, handle_search_records),
]
