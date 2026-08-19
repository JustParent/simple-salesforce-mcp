"""Tool registry: name → (Tool definition, handler).

Handlers are synchronous ``(SalesforceClient, arguments) -> str``; the server
wraps them with credential resolution and auth retry.
"""

from __future__ import annotations

from typing import Callable

from mcp.types import Tool

from ..sf_client import SalesforceClient
from . import metadata, query, records

Handler = Callable[[SalesforceClient, dict], str]

TOOL_REGISTRY: dict[str, tuple[Tool, Handler]] = {
    tool.name: (tool, handler)
    for tool, handler in [*query.TOOLS, *records.TOOLS, *metadata.TOOLS]
}
