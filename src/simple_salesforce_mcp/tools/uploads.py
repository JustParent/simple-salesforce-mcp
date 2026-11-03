"""Upload tools: attach a file to a Salesforce record.

File bytes never travel through a tool call. Two modes:

- **Sandbox mode** — when ``HARRIET_FILE_INBOX_DIR`` is set (Harriet's hosted
  sandbox, where the model's workspace is a different machine). ``prepare_file_upload``
  reserves a single-use slot ``<inbox>/<token>/`` and returns an ``upload_url`` served
  by the sandbox bridge at ``<base_url>/uploads/<token>``; the bridge writes the public
  base URL to ``<inbox>/.base_url``. The client PUTs the file there, the bridge drops it
  into the slot, and ``upload_file`` streams it from the slot to Salesforce. Tokens are
  ``<expires_at>.<random>``, as in the download outbox, so the link carries its own
  expiry.
- **Local mode** — otherwise the server runs on the user's machine, so ``upload_file``
  reads ``local_path`` directly. That reads a model-chosen path, so it needs
  ``confirm=true``, and it is refused in any sandbox, whose disk holds the
  platform-written Salesforce credentials.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp.types import Tool, ToolAnnotations

from ..formatting import to_compact_json
from ..sf_client import SalesforceClient
from ._confirm import check_confirm, missing_params
from .files import OUTBOX_DIR_ENV, _valid_id, safe_filename

INBOX_DIR_ENV = "HARRIET_FILE_INBOX_DIR"
INBOX_TTL_ENV = "HARRIET_FILE_INBOX_TTL_SECONDS"
INBOX_BASE_URL_FILE = ".base_url"
INBOX_META_FILE = ".meta.json"
DEFAULT_INBOX_TTL_SECONDS = 600

MAX_UPLOAD_BYTES_ENV = "SALESFORCE_MAX_UPLOAD_BYTES"
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_FALLBACK_NAME = "upload"
# The same token shape the sandbox bridge accepts for downloads: a 10-digit Unix expiry
# and 32-64 URL-safe random characters.
_TOKEN_RE = re.compile(r"([0-9]{10})\.[A-Za-z0-9_-]{32,64}")
_NOT_AVAILABLE = "ERROR: file uploads are not available in this environment yet."
_REPREPARE = "Call prepare_file_upload again and re-upload the file."


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _max_upload_bytes() -> int:
    return _env_int(MAX_UPLOAD_BYTES_ENV, DEFAULT_MAX_UPLOAD_BYTES)


def _inbox() -> Path | None:
    raw = os.environ.get(INBOX_DIR_ENV)
    return Path(raw) if raw else None


def _in_sandbox() -> bool:
    return bool(os.environ.get(INBOX_DIR_ENV) or os.environ.get(OUTBOX_DIR_ENV))


def _base_url(inbox: Path) -> str | None:
    try:
        base_url = (inbox / INBOX_BASE_URL_FILE).read_text().strip().rstrip("/")
    except OSError:
        return None
    return base_url if base_url.startswith("https://") else None


def _sweep_expired(inbox: Path) -> None:
    """Remove slots whose embedded expiry has passed, so abandoned uploads don't pile up."""
    now = time.time()
    try:
        for entry in inbox.iterdir():
            match = _TOKEN_RE.fullmatch(entry.name)
            if match and int(match.group(1)) < now:
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


# -- prepare_file_upload -----------------------------------------------------

PREPARE_FILE_UPLOAD = Tool(
    name="prepare_file_upload",
    description=(
        "Hosted environments only: reserve a single-use slot for sending a file from "
        "your workspace to Salesforce. The result carries an upload_url and an "
        "upload_token. Upload the file straight from the workspace right away, e.g. "
        'curl -fsS -T "<path to the file>" "<upload_url>" (HTTP PUT), then call '
        "upload_file with the upload_token to attach it to a record. Never paste or "
        "base64-encode file contents into a tool call. If the link has expired or was "
        "already used, call this tool again."
    ),
    inputSchema={
        "type": "object",
        "required": ["file_name"],
        "properties": {
            "file_name": {
                "type": "string",
                "description": (
                    "Name the file will have in Salesforce, including its extension, "
                    "e.g. contract.pdf."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Prepare file upload",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)


def handle_prepare_file_upload(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "file_name")
    if err:
        return err
    inbox = _inbox()
    if inbox is None:
        if _in_sandbox():
            return _NOT_AVAILABLE
        return (
            "ERROR: prepare_file_upload is only for hosted environments. This server runs "
            "on the user's machine, so pass local_path to upload_file instead."
        )
    base_url = _base_url(inbox)
    if base_url is None:
        return (
            "ERROR: file upload links are not available in this environment yet "
            "(no public base URL). Try again shortly."
        )

    file_name = safe_filename(str(arguments["file_name"]), fallback=_FALLBACK_NAME)
    max_bytes = _max_upload_bytes()
    ttl = _env_int(INBOX_TTL_ENV, DEFAULT_INBOX_TTL_SECONDS)
    _sweep_expired(inbox)

    expires_at = int(time.time()) + ttl
    token = f"{expires_at}.{secrets.token_urlsafe(32)}"
    # Dot-prefixed until complete, so the bridge never sees a half-made slot.
    staging = inbox / f".staging-{secrets.token_hex(16)}"
    staging.mkdir(mode=0o700)
    (staging / INBOX_META_FILE).write_text(
        json.dumps({"file_name": file_name, "max_bytes": max_bytes})
    )
    staging.rename(inbox / token)

    upload_url = f"{base_url}/uploads/{token}"
    return to_compact_json(
        {
            "upload_url": upload_url,
            "upload_token": token,
            "file_name": file_name,
            "max_bytes": max_bytes,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "expires_in_seconds": ttl,
            "single_use": True,
            "instructions": (
                f"Upload the file now (HTTP PUT; the link works once and expires in {ttl}s), "
                f'e.g. curl -fsS -T "<path to the local file>" "{upload_url}". '
                "Then call upload_file with the record_id and this upload_token. "
                "Do not paste or base64-encode the file contents."
            ),
        }
    )


# -- upload_file -------------------------------------------------------------

UPLOAD_FILE = Tool(
    name="upload_file",
    description=(
        "Attach a file (PDF, Word document, image, ...) to a Salesforce record as a "
        "Salesforce File, so it appears under the record's Files. File content is never "
        "passed in the call. In hosted environments call prepare_file_upload first, "
        "upload the file to its upload_url, then pass the upload_token here. When the "
        "server runs on the user's machine, pass local_path instead; that needs "
        "confirm=true, set only after you have shown the user the exact file and record "
        "and they have approved. Returns the new file's content_version_id (usable with "
        "download_file)."
    ),
    inputSchema={
        "type": "object",
        "required": ["record_id"],
        "properties": {
            "record_id": {
                "type": "string",
                "description": (
                    "15- or 18-character Id of the record to attach the file to (Account, "
                    "Case, Opportunity, ...). Resolve a name to an Id with run_soql_query "
                    "first."
                ),
            },
            "upload_token": {
                "type": "string",
                "description": (
                    "Hosted environments: the upload_token from prepare_file_upload, after "
                    "the file has been uploaded to its upload_url."
                ),
            },
            "local_path": {
                "type": "string",
                "description": (
                    "Standalone only: absolute path of a file on the machine running this "
                    "server. Requires confirm=true."
                ),
            },
            "title": {
                "type": "string",
                "description": "Display title. Defaults to the file name without its extension.",
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Only for local_path. Must be literally true. Before setting it, show "
                    "the user the exact file path and record and get their approval."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Upload file to record",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)


@dataclass
class _Upload:
    path: Path
    file_name: str
    size: int
    # The sandbox slot to remove once the file is in Salesforce; None for local files.
    slot: Path | None = None


def _from_slot(token: str) -> _Upload | str:
    inbox = _inbox()
    if inbox is None:
        if _in_sandbox():
            return _NOT_AVAILABLE
        return (
            "ERROR: upload_token is only used in hosted environments. This server runs on "
            "the user's machine, so pass local_path instead."
        )
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        return (
            "ERROR: upload_token is not valid. Use the upload_token returned by "
            "prepare_file_upload."
        )
    if int(match.group(1)) < time.time():
        return f"ERROR: this upload_token has expired. {_REPREPARE}"
    slot = inbox / token
    try:
        meta = json.loads((slot / INBOX_META_FILE).read_text())
    except (OSError, ValueError):
        return f"ERROR: unknown or already used upload_token. {_REPREPARE}"
    name = meta.get("file_name") if isinstance(meta, dict) else None
    file_name = safe_filename(name, fallback=_FALLBACK_NAME)
    path = slot / file_name
    if path.is_symlink() or not path.is_file():
        return (
            "ERROR: no file has been uploaded for this upload_token yet. Upload it to the "
            "upload_url first (HTTP PUT), then call upload_file again."
        )
    return _Upload(path, file_name, path.stat().st_size, slot)


def _from_local_path(local_path: str) -> _Upload | str:
    if _in_sandbox():
        if _inbox() is None:
            return _NOT_AVAILABLE
        return (
            "ERROR: local_path is not available in this environment. Call "
            "prepare_file_upload, upload the file to its upload_url, then pass the "
            "upload_token."
        )
    path = Path(local_path)
    if not path.is_file():
        return f"ERROR: no such file: {path}"
    return _Upload(path, safe_filename(path.name, fallback=_FALLBACK_NAME), path.stat().st_size)


def handle_upload_file(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "record_id")
    if err:
        return err
    record_id = _valid_id(arguments["record_id"])
    if record_id is None:
        return "ERROR: record_id must be a 15- or 18-character Salesforce Id."
    token = arguments.get("upload_token")
    local_path = arguments.get("local_path")
    if bool(token) == bool(local_path):
        return "ERROR: provide exactly one of upload_token (hosted) or local_path (standalone)."

    upload = _from_slot(str(token)) if token else _from_local_path(str(local_path))
    if isinstance(upload, str):
        return upload

    max_bytes = _max_upload_bytes()
    if upload.size > max_bytes:
        return f"ERROR: the file is {upload.size} bytes, over the {max_bytes}-byte upload limit."

    if upload.slot is None:
        gate = check_confirm(
            arguments,
            f"upload the local file {upload.path} ({upload.size} bytes) to Salesforce "
            f"record {record_id}, where everyone who can see that record can open it",
        )
        if gate:
            return gate

    title = str(arguments.get("title") or "").strip() or Path(upload.file_name).stem
    try:
        with open(upload.path, "rb") as file:
            created = client.upload_content_version(
                file, title=title, file_name=upload.file_name, parent_id=record_id
            )
    except httpx.TransportError as exc:
        # The request may have reached Salesforce before the connection failed, and the
        # generic error would tell the model to simply retry, which could attach it twice.
        return (
            f"ERROR: the upload did not finish ({type(exc).__name__}). The file may or may "
            f"not have been attached to {record_id}: check list_record_files before retrying."
        )
    if upload.slot is not None:
        shutil.rmtree(upload.slot, ignore_errors=True)
    return to_compact_json(
        {
            "content_version_id": created.get("id"),
            "file_name": upload.file_name,
            "size": upload.size,
            "linked_to": record_id,
        }
    )


TOOLS = [
    (PREPARE_FILE_UPLOAD, handle_prepare_file_upload),
    (UPLOAD_FILE, handle_upload_file),
]
