# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from mcp.types import Tool

from loom_plan.server import APP_MIME_TYPE, NEXT_UI_URI, WRITE_RULE, mcp

READ_TOOLS = {"next", "overdue", "agenda", "tasks", "find", "free_slots", "containers"}
WRITE_TOOLS = {"add_event", "add_task", "update_event", "update_task", "set_status", "delete"}


def _read_only(tool: Tool) -> bool | None:
    assert tool.annotations is not None
    return tool.annotations.readOnlyHint


async def test_tools_exposed_with_spec_rules() -> None:
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in WRITE_TOOLS:
        assert WRITE_RULE in (tools[name].description or "")
        assert _read_only(tools[name]) is False
    for name in READ_TOOLS:
        assert _read_only(tools[name]) is True
    assert tools["delete"].annotations is not None
    assert tools["delete"].annotations.destructiveHint is True
    # next must carry the rendering contract for Claude
    assert "En retard" in (tools["next"].description or "")
    assert "scope" in tools["update_event"].inputSchema["properties"]


async def test_next_has_an_mcp_app_view() -> None:
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["next"].meta == {"ui": {"resourceUri": NEXT_UI_URI}}
    res = {str(r.uri): r for r in await mcp.list_resources()}
    assert res[NEXT_UI_URI].mimeType == APP_MIME_TYPE
    contents = await mcp.read_resource(NEXT_UI_URI)
    html = next(iter(contents)).content
    assert isinstance(html, str) and "ui/initialize" in html and "tools/call" in html
