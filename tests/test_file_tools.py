import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import json_response

from simple_salesforce_mcp.sf_client import SalesforceApiError
from simple_salesforce_mcp.tools import files
from simple_salesforce_mcp.tools.files import (
    handle_download_file,
    handle_list_record_files,
    safe_filename,
)

CASE_ID = "500xx0000001234AAA"
EMAIL_ID = "02sxx0000000001AAA"
VERSION_ID = "068xx0000000001AAA"
DOCUMENT_ID = "069xx0000000001AAA"
ATTACHMENT_ID = "00Pxx0000000001AAA"
BLOB = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 400


def _soql(request: httpx.Request) -> str:
    return request.url.params.get("q") or ""


def _records(*rows) -> httpx.Response:
    return json_response({"totalSize": len(rows), "done": True, "records": list(rows)})


def _doc_link(parent_id: str, title: str) -> dict:
    return {
        "ContentDocumentId": DOCUMENT_ID,
        "LinkedEntityId": parent_id,
        "ContentDocument": {
            "Title": title,
            "FileExtension": "jpg",
            "FileType": "JPG",
            "ContentSize": 40149,
            "LatestPublishedVersionId": VERSION_ID,
            "CreatedBy": {"Name": "Martin Främke"},
            "CreatedDate": "2026-09-01T09:12:00.000+0000",
        },
    }


def test_list_record_files_merges_files_attachments_and_case_emails(make_client):
    def handler(request):
        soql = _soql(request)
        if "FROM ContentDocumentLink" in soql and CASE_ID in soql:
            return _records(_doc_link(CASE_ID, "2026-09-01_behaviour"))
        if "FROM ContentDocumentLink" in soql and EMAIL_ID in soql:
            return _records(_doc_link(EMAIL_ID, "screenshot"))
        if "FROM Attachment" in soql and CASE_ID in soql:
            return _records(
                {
                    "Id": ATTACHMENT_ID,
                    "ParentId": CASE_ID,
                    "Name": "log.txt",
                    "ContentType": "text/plain",
                    "BodyLength": 12,
                    "CreatedBy": {"Name": "A"},
                    "CreatedDate": "2026-09-02",
                }
            )
        if "FROM Attachment" in soql:
            return _records()
        if "FROM EmailMessage" in soql:
            return _records({"Id": EMAIL_ID, "Subject": "Re: mismatch"})
        pytest.fail(f"unexpected query: {soql}")

    with make_client(handler) as client:
        result = json.loads(handle_list_record_files(client, {"record_id": CASE_ID}))

    assert result["count"] == 3
    by_source = {f["source"]: f for f in result["files"]}
    assert by_source["file"]["file_id"] == VERSION_ID
    assert by_source["file"]["name"] == "2026-09-01_behaviour.jpg"
    assert by_source["attachment"]["file_id"] == ATTACHMENT_ID
    assert by_source["email: Re: mismatch"]["name"] == "screenshot.jpg"


def test_list_record_files_notes_unavailable_email_messages(make_client):
    def handler(request):
        if "FROM EmailMessage" in _soql(request):
            return json_response(
                [{"errorCode": "INVALID_TYPE", "message": "sObject type 'EmailMessage'"}], 400
            )
        return _records()

    with make_client(handler) as client:
        result = json.loads(handle_list_record_files(client, {"record_id": CASE_ID}))

    assert result["count"] == 0
    assert "Email attachments were not checked" in result["note"]


def test_list_record_files_skips_emails_for_non_case_records(make_client):
    def handler(request):
        assert "EmailMessage" not in _soql(request)
        return _records()

    with make_client(handler) as client:
        result = json.loads(handle_list_record_files(client, {"record_id": "001xx0000000001"}))
    assert result == {"count": 0, "files": []}


def test_list_record_files_rejects_injection(make_client):
    def handler(request):
        pytest.fail("no request expected")

    with make_client(handler) as client:
        out = handle_list_record_files(client, {"record_id": "500' OR Id != '"})
    assert out.startswith("ERROR")


def _download_handler(seen: dict | None = None):
    def handler(request):
        if request.url.path.endswith("/VersionData") or request.url.path.endswith("/Body"):
            if seen is not None:
                seen["blob_path"] = request.url.path
            return httpx.Response(200, content=BLOB)
        soql = _soql(request)
        if "FROM ContentDocument " in soql:
            return _records({"LatestPublishedVersionId": VERSION_ID})
        if "FROM ContentVersion" in soql:
            return _records(
                {
                    "Id": VERSION_ID,
                    "Title": "2026-09-01_behaviour",
                    "FileExtension": "jpg",
                    "PathOnClient": "2026-09-01_behaviour.jpg",
                    "ContentSize": len(BLOB),
                }
            )
        if "FROM Attachment" in soql:
            return _records(
                {
                    "Id": ATTACHMENT_ID,
                    "Name": "../../etc/passwd",
                    "ContentType": "text/plain",
                    "BodyLength": len(BLOB),
                }
            )
        pytest.fail(f"unexpected request: {request.url}")

    return handler


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    directory = tmp_path / "outbox"
    directory.mkdir()
    (directory / ".base_url").write_text("https://sb-123.modal.host\n")
    monkeypatch.setenv("HARRIET_FILE_OUTBOX_DIR", str(directory))
    return directory


def test_download_file_writes_to_outbox_and_returns_link(make_client, outbox):
    before = int(time.time())
    with make_client(_download_handler()) as client:
        out = handle_download_file(client, {"file_id": VERSION_ID})
    after = int(time.time())

    result = json.loads(out)
    assert "data" not in result
    assert len(out) < 2000  # the tool result carries metadata only, never the bytes
    token = result["download_url"].rsplit("/", 1)[-1]
    assert result["download_url"] == f"https://sb-123.modal.host/files/{token}"
    assert result["single_use"] is True
    assert result["expires_in_seconds"] == 120
    assert result["size"] == len(BLOB)
    assert result["sha256"] == hashlib.sha256(BLOB).hexdigest()
    assert result["content_type"] == "image/jpeg"

    # The token carries its own expiry so the bridge can check it independently.
    match = re.fullmatch(r"([0-9]{10})\.[A-Za-z0-9_-]{43}", token)
    assert match is not None
    expires_at = int(match.group(1))
    assert before + 120 <= expires_at <= after + 120
    assert result["expires_at"] == (
        datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Only the finished token dir is left; the staging dir was renamed into place.
    assert sorted(p.name for p in outbox.iterdir()) == [".base_url", token]
    token_dir = outbox / token
    assert (token_dir / "2026-09-01_behaviour.jpg").read_bytes() == BLOB
    meta = json.loads((token_dir / ".meta.json").read_text())
    assert meta == {"file_name": "2026-09-01_behaviour.jpg", "content_type": "image/jpeg"}


def test_download_file_failure_leaves_nothing_in_outbox(make_client, outbox):
    def handler(request):
        if "VersionData" in request.url.path:
            return json_response([{"errorCode": "NOT_FOUND", "message": "gone"}], 404)
        return _download_handler()(request)

    with make_client(handler) as client, pytest.raises(SalesforceApiError):
        handle_download_file(client, {"file_id": VERSION_ID})
    assert [p.name for p in outbox.iterdir()] == [".base_url"]


def test_download_file_without_base_url_errors(make_client, outbox):
    (outbox / ".base_url").unlink()

    def handler(request):
        if "VersionData" in request.url.path:
            pytest.fail("must not download without a way to hand out the file")
        return _download_handler()(request)

    with make_client(handler) as client:
        out = handle_download_file(client, {"file_id": VERSION_ID})
    assert out.startswith("ERROR")
    assert list(outbox.iterdir()) == []


def test_download_file_resolves_content_document_to_latest_version(make_client, outbox):
    seen: dict = {}
    with make_client(_download_handler(seen)) as client:
        result = json.loads(handle_download_file(client, {"file_id": DOCUMENT_ID}))
    assert result["file_id"] == VERSION_ID
    assert seen["blob_path"].endswith(f"/sobjects/ContentVersion/{VERSION_ID}/VersionData")


def test_download_attachment_locally_sanitises_name(make_client, tmp_path, monkeypatch):
    monkeypatch.delenv("HARRIET_FILE_OUTBOX_DIR", raising=False)
    monkeypatch.setenv("SALESFORCE_DOWNLOAD_DIR", str(tmp_path / "dl"))
    seen: dict = {}
    with make_client(_download_handler(seen)) as client:
        result = json.loads(handle_download_file(client, {"file_id": ATTACHMENT_ID}))

    assert seen["blob_path"].endswith(f"/sobjects/Attachment/{ATTACHMENT_ID}/Body")
    assert "download_url" not in result
    path = Path(result["local_path"])
    assert path == tmp_path / "dl" / ATTACHMENT_ID / "passwd"
    assert path.read_bytes() == BLOB
    assert result["content_type"] == "text/plain"


def test_download_file_enforces_size_limit_while_streaming(make_client, outbox, monkeypatch):
    monkeypatch.setattr(files, "DEFAULT_MAX_DOWNLOAD_BYTES", len(BLOB) - 1)

    def handler(request):
        if "FROM ContentVersion" in _soql(request):
            # Metadata under-reports the size; the stream must still be cut off.
            return _records({"Id": VERSION_ID, "Title": "big", "ContentSize": 10})
        if "VersionData" in request.url.path:
            return httpx.Response(200, stream=httpx.ByteStream(BLOB))
        pytest.fail(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        out = handle_download_file(client, {"file_id": VERSION_ID})
    assert out.startswith("ERROR")
    assert "limit" in out
    assert list(p for p in outbox.iterdir() if p.name != ".base_url") == []


def test_download_file_rejects_declared_oversize_before_streaming(make_client, outbox, monkeypatch):
    monkeypatch.setenv("SALESFORCE_MAX_DOWNLOAD_BYTES", "100")

    def handler(request):
        if "VersionData" in request.url.path:
            pytest.fail("must not stream an oversized file")
        return _download_handler()(request)

    with make_client(handler) as client:
        out = handle_download_file(client, {"file_id": VERSION_ID})
    assert out.startswith("ERROR")


def test_download_file_rejects_unsupported_ids(make_client):
    def handler(request):
        pytest.fail("no request expected")

    with make_client(handler) as client:
        assert handle_download_file(client, {"file_id": CASE_ID}).startswith("ERROR")
        assert handle_download_file(client, {"file_id": "068/../x"}).startswith("ERROR")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("C:\\Users\\me\\scan.png", "scan.png"),
        ("../../etc/passwd", "passwd"),
        (".hidden", "hidden"),
        ('a<b>c:"d|e?f*.txt', "abcdef.txt"),
        ("line\nbreak.txt", "linebreak.txt"),
        ("", "fallback"),
        ("...", "fallback"),
        ("x" * 300 + ".jpeg", "x" * 145 + ".jpeg"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw, fallback="fallback") == expected
