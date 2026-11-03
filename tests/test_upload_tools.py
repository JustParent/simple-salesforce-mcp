import json
import re
import time
from datetime import datetime, timezone

import httpx
import pytest
from conftest import json_response, parse_multipart

from simple_salesforce_mcp.server import run_with_auth_retry
from simple_salesforce_mcp.sf_client import SalesforceApiError, SalesforceClient
from simple_salesforce_mcp.tools import TOOL_REGISTRY
from simple_salesforce_mcp.tools.uploads import handle_prepare_file_upload, handle_upload_file

RECORD_ID = "001xx000003DGb2AAG"
VERSION_ID = "068xx0000000001AAA"
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 40
DEFAULT_MAX_BYTES = 100 * 1024 * 1024


@pytest.fixture(autouse=True)
def _standalone_env(monkeypatch):
    """Every test starts outside a hosted sandbox, whatever the developer's shell exports."""
    for name in (
        "HARRIET_FILE_INBOX_DIR",
        "HARRIET_FILE_OUTBOX_DIR",
        "HARRIET_FILE_INBOX_TTL_SECONDS",
        "SALESFORCE_MAX_UPLOAD_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / ".base_url").write_text("https://sb-123.modal.host\n")
    monkeypatch.setenv("HARRIET_FILE_INBOX_DIR", str(directory))
    return directory


@pytest.fixture
def local_file(tmp_path):
    path = tmp_path / "Q3 report.pdf"
    path.write_bytes(PDF)
    return path


def _no_requests(request):
    pytest.fail(f"unexpected HTTP call: {request.method} {request.url}")


def _created(seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return json_response({"id": VERSION_ID, "success": True, "errors": []}, 201)

    return handler


def _prepare(make_client, file_name="contract.pdf"):
    with make_client(_no_requests) as client:
        return json.loads(handle_prepare_file_upload(client, {"file_name": file_name}))


def _upload_to_slot(inbox, prepared, content=PDF):
    """Do what the sandbox bridge does on PUT <upload_url>: drop the file into the slot."""
    token = prepared["upload_token"]
    (inbox / token / prepared["file_name"]).write_bytes(content)
    return token


def _expired_slot(inbox, file_name="contract.pdf"):
    token = f"{int(time.time()) - 1}.{'a' * 43}"
    slot = inbox / token
    slot.mkdir()
    (slot / ".meta.json").write_text(json.dumps({"file_name": file_name}))
    (slot / file_name).write_bytes(PDF)
    return token


# -- prepare_file_upload -----------------------------------------------------


def test_prepare_file_upload_reserves_a_slot_and_returns_a_link(make_client, inbox):
    before = int(time.time())
    with make_client(_no_requests) as client:
        out = handle_prepare_file_upload(client, {"file_name": "contract.pdf"})
    after = int(time.time())

    result = json.loads(out)
    token = result["upload_token"]
    assert result["upload_url"] == f"https://sb-123.modal.host/uploads/{token}"
    assert result["file_name"] == "contract.pdf"
    assert result["max_bytes"] == DEFAULT_MAX_BYTES
    assert result["single_use"] is True
    assert result["expires_in_seconds"] == 600
    assert "curl" in result["instructions"]
    assert result["upload_url"] in result["instructions"]
    assert "upload_file" in result["instructions"]

    # Like outbox tokens, the token carries its own expiry for the bridge to check.
    match = re.fullmatch(r"([0-9]{10})\.[A-Za-z0-9_-]{43}", token)
    assert match is not None
    expires_at = int(match.group(1))
    assert before + 600 <= expires_at <= after + 600
    assert result["expires_at"] == (
        datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Only the finished slot is left: the staging dir was renamed into place.
    assert sorted(p.name for p in inbox.iterdir()) == [".base_url", token]
    meta = json.loads((inbox / token / ".meta.json").read_text())
    assert meta == {"file_name": "contract.pdf", "max_bytes": DEFAULT_MAX_BYTES}


def test_prepare_file_upload_reduces_the_name_to_a_safe_file_name(make_client, inbox):
    prepared = _prepare(make_client, "../../etc/passwd")
    assert prepared["file_name"] == "passwd"
    meta = json.loads((inbox / prepared["upload_token"] / ".meta.json").read_text())
    assert meta["file_name"] == "passwd"


def test_prepare_file_upload_honours_ttl_and_size_settings(make_client, inbox, monkeypatch):
    monkeypatch.setenv("HARRIET_FILE_INBOX_TTL_SECONDS", "30")
    monkeypatch.setenv("SALESFORCE_MAX_UPLOAD_BYTES", "2048")
    prepared = _prepare(make_client)
    assert prepared["expires_in_seconds"] == 30
    assert prepared["max_bytes"] == 2048


def test_prepare_file_upload_requires_a_file_name(make_client, inbox):
    with make_client(_no_requests) as client:
        out = handle_prepare_file_upload(client, {})
    assert out.startswith("ERROR")
    assert [p.name for p in inbox.iterdir()] == [".base_url"]


def test_prepare_file_upload_without_base_url_errors(make_client, inbox):
    (inbox / ".base_url").unlink()
    with make_client(_no_requests) as client:
        out = handle_prepare_file_upload(client, {"file_name": "a.pdf"})
    assert out.startswith("ERROR")
    assert list(inbox.iterdir()) == []


def test_prepare_file_upload_sweeps_expired_slots(make_client, inbox):
    now = int(time.time())
    expired = inbox / f"{now - 5}.{'a' * 43}"
    live = inbox / f"{now + 300}.{'b' * 43}"
    for slot in (expired, live):
        slot.mkdir()
        (slot / "left-over.pdf").write_bytes(b"x")

    prepared = _prepare(make_client)

    assert {p.name for p in inbox.iterdir()} == {".base_url", live.name, prepared["upload_token"]}


def test_prepare_file_upload_is_unavailable_without_an_inbox(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HARRIET_FILE_OUTBOX_DIR", str(tmp_path / "outbox"))
    with make_client(_no_requests) as client:
        out = handle_prepare_file_upload(client, {"file_name": "a.pdf"})
    assert out.startswith("ERROR")
    assert "not available" in out
    assert not (tmp_path / "outbox").exists()


def test_prepare_file_upload_points_standalone_users_at_local_path(make_client):
    with make_client(_no_requests) as client:
        out = handle_prepare_file_upload(client, {"file_name": "a.pdf"})
    assert out.startswith("ERROR")
    assert "local_path" in out


# -- upload_file: hosted (upload_token) ---------------------------------------


def test_upload_file_attaches_the_uploaded_file_to_the_record(make_client, inbox):
    token = _upload_to_slot(inbox, _prepare(make_client))
    seen = []
    with make_client(_created(seen)) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})

    assert json.loads(out) == {
        "content_version_id": VERSION_ID,
        "file_name": "contract.pdf",
        "size": len(PDF),
        "linked_to": RECORD_ID,
    }
    assert len(out) < 1000  # the result carries metadata only, never the bytes
    assert len(seen) == 1
    parts = parse_multipart(seen[0])
    assert json.loads(parts["entity_content"][1]) == {
        "Title": "contract",
        "PathOnClient": "contract.pdf",
        "FirstPublishLocationId": RECORD_ID,
    }
    assert parts["VersionData"][1] == PDF
    # Single use: the slot is gone once the file is in Salesforce.
    assert [p.name for p in inbox.iterdir()] == [".base_url"]


def test_upload_file_uses_the_given_title(make_client, inbox):
    token = _upload_to_slot(inbox, _prepare(make_client))
    seen = []
    with make_client(_created(seen)) as client:
        handle_upload_file(
            client, {"record_id": RECORD_ID, "upload_token": token, "title": "Signed contract"}
        )
    meta = json.loads(parse_multipart(seen[0])["entity_content"][1])
    assert meta["Title"] == "Signed contract"
    assert meta["PathOnClient"] == "contract.pdf"


def test_a_failed_upload_keeps_the_slot_so_it_can_be_retried(make_client, inbox):
    token = _upload_to_slot(inbox, _prepare(make_client))

    def denied(request):
        return json_response(
            [{"message": "no access", "errorCode": "INSUFFICIENT_ACCESS_OR_READONLY"}], status=403
        )

    with make_client(denied) as client, pytest.raises(SalesforceApiError):
        handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert (inbox / token / "contract.pdf").read_bytes() == PDF

    with make_client(_created()) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert json.loads(out)["content_version_id"] == VERSION_ID


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError])
def test_an_interrupted_upload_warns_the_file_may_already_be_attached(make_client, inbox, error):
    token = _upload_to_slot(inbox, _prepare(make_client))

    def interrupted(request):
        raise error("connection lost", request=request)

    with make_client(interrupted) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})

    # A blind retry could attach the file twice, so the model is told to check first.
    assert out.startswith("ERROR")
    assert "list_record_files" in out
    assert (inbox / token / "contract.pdf").exists()


def test_auth_retry_rereads_the_file_and_uploads_it_whole(make_client, inbox):
    token = _upload_to_slot(inbox, _prepare(make_client))
    bodies = []

    def transport_handler(request):
        bodies.append(parse_multipart(request)["VersionData"][1])
        if len(bodies) == 1:
            return json_response(
                [{"message": "expired", "errorCode": "INVALID_SESSION_ID"}], status=401
            )
        return json_response({"id": VERSION_ID, "success": True, "errors": []}, 201)

    def client_factory(creds):
        return SalesforceClient(creds, transport=httpx.MockTransport(transport_handler))

    out = run_with_auth_retry(
        handle_upload_file,
        {"record_id": RECORD_ID, "upload_token": token},
        client_factory=client_factory,
    )
    assert json.loads(out)["content_version_id"] == VERSION_ID
    assert bodies == [PDF, PDF]


@pytest.mark.parametrize(
    "token",
    ["../../etc", "/etc/passwd", "..", "abc", "1234567890.short", f"1234567890.{'a' * 43}/../x"],
)
def test_upload_file_rejects_malformed_tokens(make_client, inbox, token):
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert out.startswith("ERROR")


def test_upload_file_rejects_unknown_or_used_tokens(make_client, inbox):
    token = f"{int(time.time()) + 300}.{'a' * 43}"
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert out.startswith("ERROR")
    assert "prepare_file_upload" in out


def test_upload_file_rejects_expired_tokens(make_client, inbox):
    token = _expired_slot(inbox)
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert out.startswith("ERROR")
    assert "expired" in out


def test_upload_file_before_the_file_arrives_says_to_upload_it(make_client, inbox):
    prepared = _prepare(make_client)
    with make_client(_no_requests) as client:
        out = handle_upload_file(
            client, {"record_id": RECORD_ID, "upload_token": prepared["upload_token"]}
        )
    assert out.startswith("ERROR")
    assert "upload_url" in out
    assert (inbox / prepared["upload_token"]).is_dir()


def test_upload_file_refuses_a_symlinked_upload(make_client, inbox, tmp_path):
    prepared = _prepare(make_client)
    secret = tmp_path / "secret.txt"
    secret.write_text("access token")
    (inbox / prepared["upload_token"] / prepared["file_name"]).symlink_to(secret)
    with make_client(_no_requests) as client:
        out = handle_upload_file(
            client, {"record_id": RECORD_ID, "upload_token": prepared["upload_token"]}
        )
    assert out.startswith("ERROR")


def test_upload_file_ignores_path_components_in_slot_metadata(make_client, inbox):
    prepared = _prepare(make_client)
    slot = inbox / prepared["upload_token"]
    (slot / ".meta.json").write_text(json.dumps({"file_name": "../outside.txt"}))
    (inbox / "outside.txt").write_text("not an upload")
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": slot.name})
    assert out.startswith("ERROR")


def test_upload_file_enforces_the_size_limit(make_client, inbox, monkeypatch):
    token = _upload_to_slot(inbox, _prepare(make_client))
    monkeypatch.setenv("SALESFORCE_MAX_UPLOAD_BYTES", str(len(PDF) - 1))
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert out.startswith("ERROR")
    assert "limit" in out


def test_upload_file_requires_a_valid_record_id(make_client, inbox):
    with make_client(_no_requests) as client:
        assert handle_upload_file(client, {"upload_token": "t"}).startswith("ERROR")
        injected = {"record_id": "001' OR Id != '", "upload_token": "t"}
        assert handle_upload_file(client, injected).startswith("ERROR")


@pytest.mark.parametrize("source", [{}, {"upload_token": "t", "local_path": "/tmp/x"}])
def test_upload_file_needs_exactly_one_source(make_client, inbox, source):
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, **source})
    assert out.startswith("ERROR")
    assert "exactly one" in out


def test_upload_token_is_refused_outside_hosted_sandboxes(make_client):
    token = f"{int(time.time()) + 300}.{'a' * 43}"
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "upload_token": token})
    assert out.startswith("ERROR")
    assert "local_path" in out


# -- upload_file: standalone (local_path) ------------------------------------


def test_upload_local_file_requires_confirmation(make_client, local_file):
    with make_client(_no_requests) as client:
        out = handle_upload_file(client, {"record_id": RECORD_ID, "local_path": str(local_file)})
    assert out.startswith("CONFIRMATION REQUIRED")
    assert str(local_file) in out
    assert RECORD_ID in out


def test_upload_local_file_after_confirmation(make_client, local_file):
    seen = []
    with make_client(_created(seen)) as client:
        out = handle_upload_file(
            client, {"record_id": RECORD_ID, "local_path": str(local_file), "confirm": True}
        )

    assert json.loads(out) == {
        "content_version_id": VERSION_ID,
        "file_name": "Q3 report.pdf",
        "size": len(PDF),
        "linked_to": RECORD_ID,
    }
    parts = parse_multipart(seen[0])
    meta = json.loads(parts["entity_content"][1])
    assert meta["Title"] == "Q3 report"
    assert meta["PathOnClient"] == "Q3 report.pdf"
    assert parts["VersionData"][1] == PDF
    assert local_file.read_bytes() == PDF  # the user's own file is never removed


def test_upload_local_file_rejects_missing_files_and_directories(make_client, tmp_path):
    with make_client(_no_requests) as client:
        for path in (tmp_path / "nope.pdf", tmp_path):
            out = handle_upload_file(
                client, {"record_id": RECORD_ID, "local_path": str(path), "confirm": True}
            )
            assert out.startswith("ERROR")


def test_upload_local_file_enforces_the_size_limit(make_client, local_file, monkeypatch):
    monkeypatch.setenv("SALESFORCE_MAX_UPLOAD_BYTES", "10")
    with make_client(_no_requests) as client:
        out = handle_upload_file(
            client, {"record_id": RECORD_ID, "local_path": str(local_file), "confirm": True}
        )
    assert out.startswith("ERROR")
    assert "limit" in out


@pytest.mark.parametrize("variable", ["HARRIET_FILE_INBOX_DIR", "HARRIET_FILE_OUTBOX_DIR"])
def test_local_path_is_refused_in_hosted_sandboxes(
    make_client, local_file, tmp_path, monkeypatch, variable
):
    # A sandbox holds the platform-written auth store; a model-chosen path must never
    # be read and uploaded from there.
    monkeypatch.setenv(variable, str(tmp_path / "sandbox"))
    with make_client(_no_requests) as client:
        out = handle_upload_file(
            client, {"record_id": RECORD_ID, "local_path": str(local_file), "confirm": True}
        )
    assert out.startswith("ERROR")


# -- registration ------------------------------------------------------------


def test_upload_tools_are_registered_as_non_destructive_writes():
    for name in ("prepare_file_upload", "upload_file"):
        tool, _ = TOOL_REGISTRY[name]
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
    # confirm is only needed for local_path, so it must not be a required argument.
    assert TOOL_REGISTRY["upload_file"][0].inputSchema["required"] == ["record_id"]
