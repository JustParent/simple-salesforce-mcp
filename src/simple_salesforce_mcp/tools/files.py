"""File tools: list a record's files/attachments and hand out a download link.

File bytes never travel through a tool result. ``download_file`` streams the
blob to disk and returns a small JSON envelope pointing at it:

- **Sandbox mode** — when ``HARRIET_FILE_OUTBOX_DIR`` is set (Harriet's hosted
  sandbox), the file is written to ``<outbox>/<token>/<name>`` and the result
  carries a single-use, short-lived ``download_url`` served by the sandbox
  bridge at ``<base_url>/files/<token>``. The bridge writes the public base URL
  to ``<outbox>/.base_url``.
- **Local mode** — otherwise the server runs on the user's machine, so the file
  is written under ``SALESFORCE_DOWNLOAD_DIR`` and the result carries its path.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from mcp.types import Tool, ToolAnnotations

from ..formatting import cap_records, to_compact_json
from ..sf_client import SalesforceApiError, SalesforceAuthError, SalesforceClient
from ._confirm import missing_params

OUTBOX_DIR_ENV = "HARRIET_FILE_OUTBOX_DIR"
OUTBOX_TTL_ENV = "HARRIET_FILE_OUTBOX_TTL_SECONDS"
OUTBOX_BASE_URL_FILE = ".base_url"
OUTBOX_META_FILE = ".meta.json"
DEFAULT_OUTBOX_TTL_SECONDS = 120

DOWNLOAD_DIR_ENV = "SALESFORCE_DOWNLOAD_DIR"
MAX_DOWNLOAD_BYTES_ENV = "SALESFORCE_MAX_DOWNLOAD_BYTES"
DEFAULT_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024

_CHUNK_BYTES = 64 * 1024
_MAX_FILENAME_CHARS = 150
# Emails per Case whose attachments are listed; keeps the IN (...) clause bounded.
_MAX_EMAILS = 200
_MAX_ROWS = 2000

_ID_RE = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_UNSAFE_FILENAME_CHARS = frozenset('<>:"|?*')

_CONTENT_VERSION_PREFIX = "068"
_CONTENT_DOCUMENT_PREFIX = "069"
_ATTACHMENT_PREFIX = "00P"
_CASE_PREFIX = "500"


def _valid_id(value) -> str | None:
    value = str(value or "").strip()
    return value if _ID_RE.fullmatch(value) else None


def _in_clause(ids: list[str]) -> str:
    # Ids are validated against _ID_RE before reaching here, so quoting is safe.
    return ", ".join(f"'{i}'" for i in ids)


def _query_all(client: SalesforceClient, soql: str) -> list[dict]:
    result = client.query(soql)
    records = list(result.get("records") or [])
    while result.get("nextRecordsUrl") and len(records) < _MAX_ROWS:
        result = client.query_next(result["nextRecordsUrl"])
        records.extend(result.get("records") or [])
    return records


def _name_with_extension(title: str | None, extension: str | None) -> str:
    title = title or ""
    if extension and not title.lower().endswith(f".{extension.lower()}"):
        return f"{title}.{extension}"
    return title


# -- list_record_files -------------------------------------------------------

LIST_RECORD_FILES = Tool(
    name="list_record_files",
    description=(
        "List the files and attachments on a Salesforce record: Salesforce Files "
        "(ContentDocument), legacy Attachments, and — for Cases — files attached to "
        "the Case's emails. Returns a file_id for each, to pass to download_file."
    ),
    inputSchema={
        "type": "object",
        "required": ["record_id"],
        "properties": {
            "record_id": {
                "type": "string",
                "description": (
                    "15- or 18-character Id of the parent record (Case, Account, "
                    "Opportunity, ...). Resolve e.g. a CaseNumber to an Id with "
                    "run_soql_query first."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="List record files",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)


def _linked_files(client: SalesforceClient, parent_ids: list[str], sources: dict) -> list[dict]:
    rows = _query_all(
        client,
        "SELECT ContentDocumentId, LinkedEntityId, ContentDocument.Title, "
        "ContentDocument.FileExtension, ContentDocument.FileType, "
        "ContentDocument.ContentSize, ContentDocument.LatestPublishedVersionId, "
        "ContentDocument.CreatedBy.Name, ContentDocument.CreatedDate "
        f"FROM ContentDocumentLink WHERE LinkedEntityId IN ({_in_clause(parent_ids)})",
    )
    files = []
    for row in rows:
        doc = row.get("ContentDocument") or {}
        files.append(
            {
                "file_id": doc.get("LatestPublishedVersionId"),
                "document_id": row.get("ContentDocumentId"),
                "name": _name_with_extension(doc.get("Title"), doc.get("FileExtension")),
                "file_type": doc.get("FileType"),
                "size": doc.get("ContentSize"),
                "source": sources.get(row.get("LinkedEntityId"), "file"),
                "created_by": (doc.get("CreatedBy") or {}).get("Name"),
                "created_date": doc.get("CreatedDate"),
            }
        )
    return files


def _attachments(client: SalesforceClient, parent_ids: list[str], sources: dict) -> list[dict]:
    rows = _query_all(
        client,
        "SELECT Id, ParentId, Name, ContentType, BodyLength, CreatedBy.Name, CreatedDate "
        f"FROM Attachment WHERE ParentId IN ({_in_clause(parent_ids)})",
    )
    return [
        {
            "file_id": row.get("Id"),
            "name": row.get("Name"),
            "content_type": row.get("ContentType"),
            "size": row.get("BodyLength"),
            "source": sources.get(row.get("ParentId"), "attachment"),
            "created_by": (row.get("CreatedBy") or {}).get("Name"),
            "created_date": row.get("CreatedDate"),
        }
        for row in rows
    ]


def handle_list_record_files(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "record_id")
    if err:
        return err
    record_id = _valid_id(arguments["record_id"])
    if record_id is None:
        return "ERROR: record_id must be a 15- or 18-character Salesforce Id."

    files = _linked_files(client, [record_id], {})
    files += _attachments(client, [record_id], {})

    notes = []
    if record_id.startswith(_CASE_PREFIX):
        try:
            emails = _query_all(
                client,
                "SELECT Id, Subject FROM EmailMessage "
                f"WHERE ParentId = '{record_id}' ORDER BY CreatedDate DESC LIMIT {_MAX_EMAILS}",
            )
        except SalesforceAuthError:
            raise
        except SalesforceApiError as exc:
            notes.append(f"Email attachments were not checked: {exc.for_model()}")
        else:
            if emails:
                sources = {e["Id"]: f"email: {e.get('Subject') or '(no subject)'}" for e in emails}
                email_ids = list(sources)
                files += _linked_files(client, email_ids, sources)
                files += _attachments(client, email_ids, sources)

    kept, dropped = cap_records(files)
    payload: dict = {"count": len(files), "files": kept}
    if dropped:
        payload["truncated"] = True
        notes.append(f"Showing {len(kept)} of {len(files)} files.")
    if notes:
        payload["note"] = " ".join(notes)
    return to_compact_json(payload)


# -- download_file -----------------------------------------------------------

DOWNLOAD_FILE = Tool(
    name="download_file",
    description=(
        "Download a Salesforce file or attachment (file_id from list_record_files). "
        "The file content is NOT returned: the result carries either a download_url "
        "(single-use, expires within about two minutes) or a local_path. Fetch a "
        "download_url straight into the workspace right away, e.g. "
        'curl -fsSL -o "<file_name>" "<download_url>", then work on the local file. '
        "If the link has expired or was already used, call download_file again."
    ),
    inputSchema={
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "ContentVersion Id (068...), ContentDocument Id (069..., latest "
                    "version is used), or Attachment Id (00P...)."
                ),
            },
        },
    },
    annotations=ToolAnnotations(
        title="Download file",
        readOnlyHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)


def safe_filename(name: str | None, fallback: str) -> str:
    """Reduce a user-supplied file name to a single safe path component."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in _UNSAFE_FILENAME_CHARS)
    # Leading dots would make a hidden file (the outbox reserves dotfiles).
    name = name.strip().lstrip(".").strip()
    if not name:
        name = fallback
    if len(name) > _MAX_FILENAME_CHARS:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and len(ext) <= 16:
            name = stem[: _MAX_FILENAME_CHARS - len(ext) - 1] + "." + ext
        else:
            name = name[:_MAX_FILENAME_CHARS]
    return name


def _max_download_bytes() -> int:
    try:
        return int(os.environ.get(MAX_DOWNLOAD_BYTES_ENV) or DEFAULT_MAX_DOWNLOAD_BYTES)
    except ValueError:
        return DEFAULT_MAX_DOWNLOAD_BYTES


def _outbox_ttl_seconds() -> int:
    try:
        return int(os.environ.get(OUTBOX_TTL_ENV) or DEFAULT_OUTBOX_TTL_SECONDS)
    except ValueError:
        return DEFAULT_OUTBOX_TTL_SECONDS


def _resolve_file(client: SalesforceClient, file_id: str) -> dict | str:
    """Return ``{object_type, id, field, name, content_type, size}`` or an error string."""
    prefix = file_id[:3]
    if prefix == _CONTENT_DOCUMENT_PREFIX:
        rows = _query_all(
            client, f"SELECT LatestPublishedVersionId FROM ContentDocument WHERE Id = '{file_id}'"
        )
        version_id = rows[0].get("LatestPublishedVersionId") if rows else None
        if not version_id:
            return f"ERROR: no accessible file with Id {file_id}."
        file_id, prefix = version_id, _CONTENT_VERSION_PREFIX

    if prefix == _CONTENT_VERSION_PREFIX:
        rows = _query_all(
            client,
            "SELECT Id, Title, FileExtension, PathOnClient, ContentSize "
            f"FROM ContentVersion WHERE Id = '{file_id}'",
        )
        if not rows:
            return f"ERROR: no accessible file with Id {file_id}."
        row = rows[0]
        name = _name_with_extension(row.get("Title"), row.get("FileExtension"))
        if not name:
            name = str(row.get("PathOnClient") or "")
        return {
            "object_type": "ContentVersion",
            "id": row.get("Id") or file_id,
            "field": "VersionData",
            "name": name,
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": row.get("ContentSize"),
        }

    if prefix == _ATTACHMENT_PREFIX:
        rows = _query_all(
            client,
            f"SELECT Id, Name, ContentType, BodyLength FROM Attachment WHERE Id = '{file_id}'",
        )
        if not rows:
            return f"ERROR: no accessible attachment with Id {file_id}."
        row = rows[0]
        name = str(row.get("Name") or "")
        return {
            "object_type": "Attachment",
            "id": row.get("Id") or file_id,
            "field": "Body",
            "name": name,
            "content_type": row.get("ContentType")
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream",
            "size": row.get("BodyLength"),
        }

    return (
        "ERROR: file_id must be a ContentVersion (068...), ContentDocument (069...), or "
        "Attachment (00P...) Id. Use list_record_files to find it."
    )


class _TooLarge(Exception):
    pass


def _stream_to(client: SalesforceClient, info: dict, path: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    with client.stream_blob(info["object_type"], info["id"], info["field"]) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise _TooLarge
        with open(path, "wb") as out:
            for chunk in response.iter_bytes(_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise _TooLarge
                digest.update(chunk)
                out.write(chunk)
    return written, digest.hexdigest()


def _outbox() -> tuple[Path, str | None] | None:
    raw = os.environ.get(OUTBOX_DIR_ENV)
    if not raw:
        return None
    directory = Path(raw)
    try:
        base_url = (directory / OUTBOX_BASE_URL_FILE).read_text().strip().rstrip("/")
    except OSError:
        base_url = ""
    return directory, (base_url if base_url.startswith("https://") else None)


def handle_download_file(client: SalesforceClient, arguments: dict) -> str:
    err = missing_params(arguments, "file_id")
    if err:
        return err
    file_id = _valid_id(arguments["file_id"])
    if file_id is None:
        return "ERROR: file_id must be a 15- or 18-character Salesforce Id."

    info = _resolve_file(client, file_id)
    if isinstance(info, str):
        return info

    max_bytes = _max_download_bytes()
    size = info["size"]
    if isinstance(size, int) and size > max_bytes:
        return (
            f"ERROR: the file is {size} bytes, over the {max_bytes}-byte download limit. "
            "Ask the user to download it from Salesforce directly."
        )

    file_name = safe_filename(info["name"], fallback=info["id"])
    outbox = _outbox()
    if outbox is not None:
        outbox_dir, base_url = outbox
        if base_url is None:
            return (
                "ERROR: file download links are not available in this environment yet "
                "(no public base URL). Try again shortly."
            )
        token = secrets.token_urlsafe(32)
        target_dir = outbox_dir / token
        target_dir.mkdir(mode=0o700, parents=True)
    else:
        base_url = token = None
        root = Path(
            os.environ.get(DOWNLOAD_DIR_ENV)
            or Path(tempfile.gettempdir()) / "simple-salesforce-mcp"
        )
        target_dir = root / info["id"]
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    path = target_dir / file_name
    try:
        written, sha256 = _stream_to(client, info, path, max_bytes)
    except _TooLarge:
        shutil.rmtree(target_dir, ignore_errors=True)
        return (
            f"ERROR: the file exceeds the {max_bytes}-byte download limit. "
            "Ask the user to download it from Salesforce directly."
        )
    except BaseException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    payload: dict = {
        "file_id": info["id"],
        "file_name": file_name,
        "content_type": info["content_type"],
        "size": written,
        "sha256": sha256,
    }
    if token is not None:
        # Written last: the bridge measures the link's age from this file.
        (target_dir / OUTBOX_META_FILE).write_text(
            json.dumps({"file_name": file_name, "content_type": info["content_type"]})
        )
        ttl = _outbox_ttl_seconds()
        payload["download_url"] = f"{base_url}/files/{token}"
        payload["expires_in_seconds"] = ttl
        payload["single_use"] = True
        payload["instructions"] = (
            f"Download now (link works once, expires in {ttl}s), e.g. "
            f'curl -fsSL -o "{file_name}" "{payload["download_url"]}". '
            "Then inspect the local file; do not paste its contents into the chat."
        )
    else:
        payload["local_path"] = str(path)
        payload["instructions"] = (
            "The file was saved on the machine running this server; open it from local_path."
        )
    return to_compact_json(payload)


TOOLS = [
    (LIST_RECORD_FILES, handle_list_record_files),
    (DOWNLOAD_FILE, handle_download_file),
]
