# simple-salesforce-mcp

[![Tests](https://github.com/JustParent/simple-salesforce-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/JustParent/simple-salesforce-mcp/actions/workflows/tests.yml)

A lightweight Salesforce [MCP](https://modelcontextprotocol.io) server. Talks straight to the
Salesforce REST API over stdio with two runtime dependencies (`mcp`, `httpx`) — built as a
fast-cold-start replacement for the official `@salesforce/mcp` DX server, whose dependency
tree is too heavy for sandboxed environments.

## Tools

| Tool | Writes? | Notes |
|---|---|---|
| `run_soql_query` | no | Raw SOQL escape hatch with pagination (`next_url`) |
| `search_records` | no | Cross-object full-text search (parameterized search API) |
| `get_record` | no | Fetch one record by Id, optional field list |
| `create_record` | yes | Create a record |
| `update_record` | yes, **confirm-gated** | Requires literal `confirm=true` |
| `delete_record` | yes, **confirm-gated** | Requires literal `confirm=true` |
| `describe_object` | no | Trimmed object metadata (fields, types, required, picklists) |
| `list_objects` | no | Org objects, system noise filtered by default |
| `get_org_info` | no | Current user, org, instance URL, API version |

Every tool carries MCP annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint`).
`update_record` and `delete_record` refuse to act unless the call includes `confirm: true`,
and the refusal message instructs the model to present the exact change to the user and get
approval first. SOQL itself cannot modify data, so the escape hatch stays read-only.

## Authentication

The server performs no OAuth flow itself; it consumes an existing access token, resolved
**on every tool call** (so an external process may rotate the token at any time):

1. **Salesforce CLI auth store** under `$HOME`:
   - `~/.sf/config.json` → `{"target-org": "<username>"}` (or legacy
     `~/.sfdx/sfdx-config.json` → `{"defaultusername": ...}`)
   - `~/.sfdx/<username>.json` → `{"accessToken": "...", "instanceUrl": "https://..."}`

   This is the shape written by `sf org login`, and the shape the JustParent platform
   materialises into sandboxes (with the platform refreshing the access token before each
   call). Refresh tokens and client secrets in these files are ignored — never read into
   memory, stored, or logged.
2. **Environment variables** (fallback, for local/standalone use):
   `SALESFORCE_ACCESS_TOKEN` and `SALESFORCE_INSTANCE_URL`.

On a rejected token (HTTP 401 / `INVALID_SESSION_ID`) the server re-reads the auth store
once and retries, then reports that the connection needs to be re-established.

The REST API version defaults to `62.0`; override with `SALESFORCE_API_VERSION`.

## Running

```bash
# Standalone with env vars (get a token via: sf org display --json)
SALESFORCE_ACCESS_TOKEN=... \
SALESFORCE_INSTANCE_URL=https://yourorg.my.salesforce.com \
uvx --from git+https://github.com/JustParent/simple-salesforce-mcp simple-salesforce-mcp
```

Claude Desktop / generic MCP client config:

```json
{
  "mcpServers": {
    "salesforce": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/JustParent/simple-salesforce-mcp", "simple-salesforce-mcp"],
      "env": {
        "SALESFORCE_ACCESS_TOKEN": "...",
        "SALESFORCE_INSTANCE_URL": "https://yourorg.my.salesforce.com"
      }
    }
  }
}
```

Smoke test (no network, prints version, credential status, and tools; exits 0):

```bash
uvx --from . simple-salesforce-mcp --test
```

## Development

```bash
uv run pytest                    # unit tests, no network needed
uv run ruff check .              # lint
uv run ruff format .             # format (--check to verify only)
uv run mypy                      # type check (src and tests)
uv run simple-salesforce-mcp --test
npx @modelcontextprotocol/inspector uv run simple-salesforce-mcp   # interactive
```

CI (`.github/workflows/tests.yml`) runs on every push and pull request: the test
suite on Python 3.10–3.13, ruff lint and format checks plus mypy, and a packaging
job that installs the package the way sandboxes do (`uvx --from .`) to catch
packaging breakage.

## Security notes

- Access-token only: the server never requests, reads, or persists refresh tokens.
- Destructive operations (update/delete) are gated behind an explicit `confirm=true`
  argument with model-facing guidance to obtain user approval first.
- SOQL pagination cursors (`next_url`) are validated to be `/services/data/...` paths, and
  object/record identifiers are validated before being placed in URLs.
- Responses are size-capped with explicit truncation notices.
