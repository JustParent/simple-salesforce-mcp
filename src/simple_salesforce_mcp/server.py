"""MCP server wiring: stdio transport, tool dispatch, per-call auth, retry."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from . import __version__
from .auth import CredentialsError, resolve_credentials
from .sf_client import SalesforceApiError, SalesforceAuthError, SalesforceClient
from .tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Hard backstop on response size; record-level trimming in formatting.py should
# keep well under this.
MAX_RESPONSE_CHARS = 100_000

RECONNECT_HINT = (
    "The Salesforce connection has likely expired. Ask the user to reconnect "
    "Salesforce (in JustParent: Profile → Integrations)."
)


def run_with_auth_retry(handler, arguments: dict, client_factory=SalesforceClient) -> str:
    """Resolve credentials, run the handler, and on a rejected token re-read the
    (possibly rewritten) auth files and retry exactly once.

    Safe for write handlers: a 401 means Salesforce never executed the request,
    so the retried call is the first one that actually runs.
    """
    creds = resolve_credentials()
    try:
        with client_factory(creds) as client:
            return handler(client, arguments)
    except SalesforceAuthError:
        pass
    fresh = resolve_credentials()
    try:
        with client_factory(fresh) as client:
            return handler(client, arguments)
    except SalesforceAuthError as exc:
        raise SalesforceApiError(
            "Salesforce rejected the access token twice.",
            status=exc.status,
            error_code=exc.error_code,
            hint=RECONNECT_HINT,
        ) from exc


def dispatch_tool(name: str, arguments: dict) -> str:
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return f"ERROR: unknown tool {name!r}. Available tools: {', '.join(sorted(TOOL_REGISTRY))}"
    _, handler = entry
    try:
        text = run_with_auth_retry(handler, arguments)
    except CredentialsError as exc:
        text = f"ERROR: {exc}"
    except SalesforceApiError as exc:
        text = f"ERROR: {exc.for_model()}"
    except httpx.TimeoutException:
        text = (
            "ERROR: Salesforce did not respond in time. Narrow the request "
            "(add a LIMIT, select fewer fields) and try again."
        )
    except httpx.HTTPError as exc:
        text = f"ERROR: could not reach Salesforce: {type(exc).__name__}."
    except Exception:
        logger.exception("Unhandled error in tool %s", name)
        text = "ERROR: internal server error; check the server logs."
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + (
            "\n[TRUNCATED: response exceeded the size limit. Request less data "
            "(add a LIMIT, select fewer fields, or use pagination).]"
        )
    return text


def create_server() -> Server:
    server = Server("simple-salesforce")

    @server.list_tools()
    async def list_tools():
        return [tool for tool, _ in TOOL_REGISTRY.values()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None):
        text = dispatch_tool(name, arguments or {})
        return [TextContent(type="text", text=text)]

    return server


async def _serve() -> None:
    server = create_server()
    options = InitializationOptions(
        server_name="simple-salesforce",
        server_version=__version__,
        capabilities=server.get_capabilities(NotificationOptions(), {}),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


def run_self_test() -> int:
    """Print version, credential status, and the tool list; never touches the
    network and never prints token material. Exit 0 even without credentials so
    it can serve as an install smoke test."""
    print(f"simple-salesforce-mcp {__version__}")
    try:
        creds = resolve_credentials()
    except CredentialsError:
        print("credentials: NOT CONFIGURED")
    else:
        who = f" (user {creds.username})" if creds.username else ""
        print(f"credentials: {creds.source}{who}")
    print(f"tools ({len(TOOL_REGISTRY)}):")
    for name, (tool, _) in sorted(TOOL_REGISTRY.items()):
        flags = []
        if tool.annotations is not None:
            if tool.annotations.readOnlyHint:
                flags.append("read-only")
            if tool.annotations.destructiveHint:
                flags.append("destructive")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {name}{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="simple-salesforce-mcp",
        description="Lightweight Salesforce MCP server (stdio).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="print version, credential status, and tools, then exit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    if args.test:
        return run_self_test()
    asyncio.run(_serve())
    return 0
