"""Shared confirm gate for tools that modify Salesforce data."""

from __future__ import annotations


def check_confirm(arguments: dict, action: str) -> str | None:
    """Return an error string unless ``arguments['confirm']`` is literally True."""
    if arguments.get("confirm") is True:
        return None
    return (
        "CONFIRMATION REQUIRED — no changes were made. This call would "
        f"{action}. Present the exact change to the user (object, record, and "
        "every field with its new value), wait for their explicit approval, and "
        "only then call this tool again with confirm=true. Never set "
        "confirm=true without the user's approval."
    )


def missing_params(arguments: dict, *names: str) -> str | None:
    missing = [name for name in names if not arguments.get(name)]
    if missing:
        return "ERROR: missing required parameter(s): " + ", ".join(missing)
    return None
