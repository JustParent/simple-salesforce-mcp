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
| `list_record_files` | no | Files, legacy Attachments, and (for Cases) email attachments on a record |
| `download_file` | no | Streams a file to disk and returns a link or path, never the bytes |
| `prepare_file_upload` | yes | Hosted sandboxes only: reserves a single-use upload slot and returns a link to PUT the file to |
| `upload_file` | yes, `local_path` is **confirm-gated** | Attaches a file to a record as a Salesforce File; the bytes never pass through the call |
| `list_actions` | no | Invocable actions: standard, or custom by type (flow, apex, quickAction, ...) |
| `describe_action` | no | An action's inputs (type, required, allowed values) and outputs |
| `invoke_action` | yes, **confirm-gated** | Runs an action for one or more inputs and reports each one's success or errors |

Every tool carries MCP annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint`).
`update_record` and `delete_record` refuse to act unless the call includes `confirm: true`,
and the refusal message instructs the model to present the exact change to the user and get
approval first. `upload_file` is gated the same way when given a `local_path`, since that
reads a model-chosen path on the user's machine. `invoke_action` is always gated, since
what an action does cannot be known from its name. SOQL itself cannot modify data, so the
escape hatch stays read-only.

## Invocable actions

Some changes are operations rather than field edits: a Knowledge article's `PublishStatus` is
read-only, and moving it back to draft is the "Edit as Draft" action. The three action tools
wrap Salesforce's [Actions REST API](https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/)
(`/actions/standard` and `/actions/custom/{type}`), the same actions Flow Builder offers, so
they need no per-action code:

- **Standard actions**, such as the Knowledge lifecycle (`createDraftFromOnlineKnowledgeArticle`,
  `publishKnowledgeArticles`, `archiveKnowledgeArticles`), submit for approval, and send email.
- **Custom actions**, which the org defines itself: autolaunched Flows (`flow`), Apex
  `@InvocableMethod` classes (`apex`), quick actions (`quickAction`, named `Object.Action` for
  object-scoped ones) and email alerts. An admin can expose a new operation by building an
  autolaunched Flow, with no change to this server.

The flow is `list_actions` → `describe_action` → `invoke_action`. Salesforce answers an
invocation with HTTP 200 even when some inputs fail, so `invoke_action` reports each input's
`success`, `outputs` and `errors`, and leads with `ERROR:` when every input failed or `PARTIAL:`
when some did. Up to 200 inputs go in one call.

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

## Downloading files

`download_file` never puts file content in a tool result, so any file size works and tool-call
logs only ever hold a small JSON envelope (`file_name`, `content_type`, `size`, `sha256`, plus
where to get the file). It accepts ContentVersion (`068…`), ContentDocument (`069…`, the latest
version is used) and Attachment (`00P…`) Ids, and streams the blob to disk in chunks.

- **Hosted sandbox (Harriet).** When `HARRIET_FILE_OUTBOX_DIR` is set, the file is streamed
  into a hidden `.staging-*` directory with a `.meta.json` alongside, then renamed to
  `<outbox>/<token>/<file name>` once complete. `token` is `<expires_at>.<random>`: the Unix
  expiry time (now + `HARRIET_FILE_OUTBOX_TTL_SECONDS`, default 120) and 256 random bits. The
  result carries `download_url` = `<base>/files/<token>` (with `<base>` read from
  `<outbox>/.base_url`) and `expires_at`. The sandbox bridge serves that URL **once**, then
  deletes the file. It refuses the link once the expiry in the token has passed *or* the file
  on disk is older than the TTL, since each check works without the other, and it sweeps
  expired files in the background. The client fetches the link straight into its
  workspace, e.g. `curl -fsSL -o scan.jpg "<download_url>"`.
- **Local (standalone).** Otherwise the file is saved under `SALESFORCE_DOWNLOAD_DIR`
  (default `<tmp>/simple-salesforce-mcp`) and the result carries `local_path`.

Downloads above `SALESFORCE_MAX_DOWNLOAD_BYTES` (default 500 MB) are refused.

## Uploading files

`upload_file` attaches a file (PDF, Word document, image, ...) to a record such as an Account
or Case. It creates a Salesforce File (a ContentVersion) with `FirstPublishLocationId` set to
the record, so the file appears under the record's Files with no Apex, Flow or Connected App
involved. As with downloads, file content never travels through a tool call: the file streams
from disk to Salesforce as `multipart/form-data`, and the result is a small JSON envelope
(`content_version_id`, `file_name`, `size`, `linked_to`). Files above
`SALESFORCE_MAX_UPLOAD_BYTES` (default 100 MB) are refused, and a very large file may not
finish inside the platform's per-tool-call timeout.

- **Hosted sandbox (Harriet).** The model's workspace is a different machine from the sandbox,
  so the file goes through a single-use inbox, the mirror image of the download outbox. Set
  `HARRIET_FILE_INBOX_DIR`, and the flow is:
  1. `prepare_file_upload(file_name)` creates `<inbox>/<token>/` holding `.meta.json`
     (`{"file_name": ..., "max_bytes": ...}`), built under a hidden `.staging-*` name and renamed
     into place. `token` is `<expires_at>.<random>` like the outbox's, valid for
     `HARRIET_FILE_INBOX_TTL_SECONDS` (default 600). The result carries `upload_token` and
     `upload_url` = `<base>/uploads/<token>` (with `<base>` read from `<inbox>/.base_url`, which
     must be `https://`). Expired slots are swept whenever this tool runs.
  2. The client PUTs the file from its workspace, e.g. `curl -fsS -T report.pdf "<upload_url>"`.
  3. `upload_file(record_id, upload_token)` streams `<inbox>/<token>/<file name>` to Salesforce
     and deletes the slot. A failed upload keeps the slot, so it can be retried until the
     token expires.

  **The sandbox bridge must implement the PUT side** (it lives outside this repo). Accept exactly
  one `PUT /uploads/<token>` per slot, refusing it if the expiry in the token has passed, if
  `<inbox>/<token>/.meta.json` does not exist, or if the slot already holds a file. Stream the
  body into a hidden file in the slot (refuse and discard anything above `max_bytes` from
  `.meta.json`), and only once it is complete rename it to `<inbox>/<token>/<file_name>`, taking
  `file_name` from `.meta.json`. `upload_file` only ever reads that final name, so it never sees
  a partial upload. Without `HARRIET_FILE_INBOX_DIR` a sandbox reports that uploads are not
  available yet.
- **Local (standalone).** Otherwise the server runs on the user's machine, and `upload_file`
  reads `local_path` directly. Because that is a model-chosen path, the call needs
  `confirm: true` after the user has approved the exact file and record. `local_path` is refused
  whenever `HARRIET_FILE_INBOX_DIR` or `HARRIET_FILE_OUTBOX_DIR` is set, since a sandbox's disk
  holds the platform-written Salesforce credentials.

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
- Destructive operations (update, delete, invoke action) are gated behind an explicit `confirm=true`
  argument with model-facing guidance to obtain user approval first.
- SOQL pagination cursors (`next_url`) are validated to be `/services/data/...` paths, and
  object/record identifiers and action types/names are validated before being placed in URLs.
- Responses are size-capped with explicit truncation notices.
- File downloads never travel through a tool result; hosted download links are single-use,
  short-lived, and unguessable, and file names are reduced to a safe single path component.
- File uploads never carry bytes in a tool call either. In a sandbox only a file the bridge
  placed in a server-created, single-use slot is read (tokens must match a strict format and
  symlinks are refused), and a model-chosen `local_path` is refused there and confirm-gated
  everywhere else.
