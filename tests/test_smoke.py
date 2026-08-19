from conftest import TEST_TOKEN

from simple_salesforce_mcp import __version__
from simple_salesforce_mcp.server import main
from simple_salesforce_mcp.tools import TOOL_REGISTRY


def test_self_test_without_credentials(clean_env, capsys):
    assert main(["--test"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "NOT CONFIGURED" in out
    for name in TOOL_REGISTRY:
        assert name in out
    assert "destructive" in out


def test_self_test_with_credentials_never_prints_token(fake_home, capsys):
    assert main(["--test"]) == 0
    out = capsys.readouterr().out
    assert "sfdx:" in out
    assert TEST_TOKEN not in out


def test_expected_tool_surface():
    assert sorted(TOOL_REGISTRY) == [
        "create_record",
        "delete_record",
        "describe_object",
        "get_org_info",
        "get_record",
        "list_objects",
        "run_soql_query",
        "search_records",
        "update_record",
    ]


def test_annotations_are_set_on_every_tool():
    for name, (tool, _) in TOOL_REGISTRY.items():
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is not None, name
    read_only = {name for name, (tool, _) in TOOL_REGISTRY.items() if tool.annotations.readOnlyHint}
    assert read_only == {
        "run_soql_query",
        "search_records",
        "get_record",
        "describe_object",
        "list_objects",
        "get_org_info",
    }
    destructive = {
        name for name, (tool, _) in TOOL_REGISTRY.items() if tool.annotations.destructiveHint
    }
    assert destructive == {"update_record", "delete_record"}


def test_confirm_is_required_in_write_schemas():
    for name in ("update_record", "delete_record"):
        tool, _ = TOOL_REGISTRY[name]
        assert "confirm" in tool.inputSchema["required"], name
