"""Tests for Workspace MCP Server stdio app, routing, and biometric enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from workspace.exceptions import BiometricHardwareUnavailable, BiometricVerificationDenied
from workspace_mcp.config import MCPConfig
from workspace_mcp.server import (
    WorkspaceMCPServer,
    create_server,
    generate_tool_schemas,
)
from workspace_mcp.tools import WRITE_TOOL_NAMES


@pytest.fixture
def mock_mcp_server(monkeypatch: pytest.MonkeyPatch) -> WorkspaceMCPServer:
    monkeypatch.setenv("WORKSPACE_EMAIL", "test@example.com")
    monkeypatch.setenv("WORKSPACE_APP_PASSWORD", "test-secret")
    config = MCPConfig(enable_write_tools=True, require_biometric=True)
    server = WorkspaceMCPServer(config)
    return server


@pytest.mark.asyncio
async def test_list_tools_all_enabled(mock_mcp_server: WorkspaceMCPServer) -> None:
    app = create_server(mock_mcp_server)
    list_handler = app.request_handlers[ListToolsRequest]
    res = await list_handler(ListToolsRequest(method="tools/list"))
    tool_names = {t.name for t in res.root.tools}
    assert len(tool_names) == 8
    assert "check_connection_status" in tool_names
    assert "search_emails" in tool_names
    assert "get_email" in tool_names
    assert "send_email" in tool_names
    assert "list_calendars" in tool_names
    assert "list_calendar_events" in tool_names
    assert "create_calendar_event" in tool_names
    assert "delete_calendar_event" in tool_names


def test_tool_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_EMAIL", "test@example.com")
    monkeypatch.setenv("WORKSPACE_APP_PASSWORD", "test-secret")

    # When enable_write_tools is False
    cfg_readonly = MCPConfig(enable_write_tools=False)
    server_readonly = WorkspaceMCPServer(cfg_readonly)
    for write_tool in WRITE_TOOL_NAMES:
        assert server_readonly.tool_is_enabled(write_tool) is False
    assert server_readonly.tool_is_enabled("search_emails") is True
    assert server_readonly.tool_is_enabled("list_calendars") is True

    # When enable_write_tools is True
    cfg_write = MCPConfig(enable_write_tools=True)
    server_write = WorkspaceMCPServer(cfg_write)
    for write_tool in WRITE_TOOL_NAMES:
        assert server_write.tool_is_enabled(write_tool) is True


@pytest.mark.asyncio
async def test_call_tool_requires_confirmation(
    mock_mcp_server: WorkspaceMCPServer,
) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="send_email",
            arguments={"to": ["a@b.com"], "subject": "Hi", "body": "Msg", "confirm": False},
        ),
    )
    res = await call_handler(req)
    assert res.root.isError is True
    payload = json.loads(res.root.content[0].text)
    assert payload["error"] == "ConfirmationRequired"


@pytest.mark.asyncio
async def test_call_tool_biometric_denied(
    mock_mcp_server: WorkspaceMCPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    def mock_verify(*args, **kwargs):
        raise BiometricVerificationDenied("User rejected Touch ID prompt.")

    monkeypatch.setattr("workspace_mcp.server.verify_user_presence", mock_verify)

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="send_email",
            arguments={"to": ["a@b.com"], "subject": "Hi", "body": "Msg", "confirm": True},
        ),
    )
    res = await call_handler(req)
    assert res.root.isError is True
    payload = json.loads(res.root.content[0].text)
    assert payload["error"] == "BiometricDenied"
    assert "User rejected" in payload["message"]


@pytest.mark.asyncio
async def test_call_tool_biometric_unavailable(
    mock_mcp_server: WorkspaceMCPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    def mock_verify(*args, **kwargs):
        raise BiometricHardwareUnavailable("Biometrics not configured.")

    monkeypatch.setattr("workspace_mcp.server.verify_user_presence", mock_verify)

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="send_email",
            arguments={"to": ["a@b.com"], "subject": "Hi", "body": "Msg", "confirm": True},
        ),
    )
    res = await call_handler(req)
    assert res.root.isError is True
    payload = json.loads(res.root.content[0].text)
    assert payload["error"] == "BiometricUnavailable"


@pytest.mark.asyncio
async def test_call_tool_read_tool_success(
    mock_mcp_server: WorkspaceMCPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    mock_mcp_server.gmail_client.search_emails = MagicMock(return_value=[])

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="search_emails", arguments={"query": "test"}),
    )
    res = await call_handler(req)
    assert res.root.isError is not True
    payload = json.loads(res.root.content[0].text)
    assert payload["count"] == 0
    assert payload["emails"] == []


def test_generate_tool_schemas(tmp_path: Path) -> None:
    out_dir = tmp_path / "schemas"
    generate_tool_schemas(out_dir)

    assert (out_dir / "instructions.md").exists()
    assert (out_dir / "check_connection_status.json").exists()
    assert (out_dir / "search_emails.json").exists()
    assert (out_dir / "get_email.json").exists()
    assert (out_dir / "send_email.json").exists()
    assert (out_dir / "list_calendars.json").exists()
    assert (out_dir / "list_calendar_events.json").exists()
    assert (out_dir / "create_calendar_event.json").exists()
    assert (out_dir / "delete_calendar_event.json").exists()

    # Verify JSON structure
    data = json.loads((out_dir / "send_email.json").read_text(encoding="utf-8"))
    assert data["name"] == "send_email"
    assert "parameters" in data
    assert "to" in data["parameters"]["properties"]
    assert "confirm" in data["parameters"]["properties"]


@pytest.mark.asyncio
async def test_call_tool_write_tool_success(
    mock_mcp_server: WorkspaceMCPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workspace.models import SendEmailResult
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    monkeypatch.setattr("workspace_mcp.server.verify_user_presence", lambda prompt, timeout: True)
    mock_mcp_server.gmail_client.send_email = MagicMock(
        return_value=SendEmailResult(success=True, action="send_email", to=["a@b.com"], subject="Hi")
    )

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="send_email",
            arguments={"to": ["a@b.com"], "subject": "Hi", "body": "Body", "confirm": True},
        ),
    )
    res = await call_handler(req)
    assert not res.root.isError
    payload = json.loads(res.root.content[0].text)
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_call_tool_not_found(mock_mcp_server: WorkspaceMCPServer) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="nonexistent_tool", arguments={}),
    )
    res = await call_handler(req)
    assert res.root.isError is True
    payload = json.loads(res.root.content[0].text)
    assert payload["error"] == "ToolNotFound"


@pytest.mark.asyncio
async def test_call_tool_validation_error(mock_mcp_server: WorkspaceMCPServer) -> None:
    app = create_server(mock_mcp_server)
    call_handler = app.request_handlers[CallToolRequest]

    # Missing required argument 'email_id' in get_email
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="get_email", arguments={}),
    )
    res = await call_handler(req)
    assert res.root.isError is True
    assert "required property" in res.root.content[0].text or "ValidationError" in res.root.content[0].text


def test_main_cli_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from workspace_mcp.server import main

    # 1. --generate-schemas
    out_dir = tmp_path / "cli_schemas"
    monkeypatch.setattr("sys.argv", ["workspace-mcp", "--generate-schemas", str(out_dir)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert (out_dir / "send_email.json").exists()

    # 2. --test-biometric success
    monkeypatch.setattr("sys.argv", ["workspace-mcp", "--test-biometric"])
    monkeypatch.setattr("workspace_mcp.server.verify_user_presence", lambda msg: True)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    # 3. --test-connection
    monkeypatch.setattr("sys.argv", ["workspace-mcp", "--test-connection"])
    monkeypatch.setenv("WORKSPACE_EMAIL", "test@example.com")
    monkeypatch.setenv("WORKSPACE_APP_PASSWORD", "test-pass")
    monkeypatch.setattr("workspace.gmail.GmailClient.check_connection", lambda self: {"imap": True, "smtp": True, "errors": {}})
    monkeypatch.setattr("workspace.calendar.CalendarClient.discover_calendars", lambda self: [])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    # 4. --test-connection error branch
    monkeypatch.setattr("workspace.gmail.GmailClient.check_connection", lambda self: (_ for _ in ()).throw(RuntimeError("fail")))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
