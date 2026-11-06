"""Tests for Google Workspace MCP tools logic and schema validation."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from workspace.models import (
    BatchMutationItemResult,
    BatchMutationsResult,
    CalendarEvent,
    CalendarInfo,
    CreateEventResult,
    DeleteEventResult,
    EmailMessage,
    MoveEventResult,
    SendEmailResult,
    UpdateEventResult,
)
from workspace_mcp.tools.calendar import (
    BatchCalendarMutationsTool,
    CreateCalendarEventTool,
    DeleteCalendarEventTool,
    ListCalendarEventsTool,
    ListCalendarsTool,
    MoveCalendarEventTool,
    UpdateCalendarEventTool,
)
from workspace_mcp.tools.email import (
    GetEmailTool,
    SearchEmailsTool,
    SendEmailTool,
)
from workspace_mcp.tools.status import CheckConnectionStatusTool


@pytest.mark.asyncio
async def test_check_connection_status_tool() -> None:
    mock_gmail = MagicMock()
    mock_gmail.config.email = "operator@example.com"
    mock_gmail.config.imap_server = "imap.example.com"
    mock_gmail.config.smtp_server = "smtp.example.com"
    mock_gmail.check_connection.return_value = {"imap": True, "smtp": True, "errors": {}}

    mock_cal = MagicMock()
    mock_cal.discover_calendars.return_value = [
        CalendarInfo(name="Primary", href="/dav/events/", url="https://cal.example.com")
    ]

    tool = CheckConnectionStatusTool()
    res = await tool.execute(mock_gmail, mock_cal, {})
    assert res["email"] == "operator@example.com"
    assert res["imap_ok"] is True
    assert res["smtp_ok"] is True
    assert res["caldav_ok"] is True
    assert res["calendars_count"] == 1


@pytest.mark.asyncio
async def test_check_connection_status_caldav_error() -> None:
    mock_gmail = MagicMock()
    mock_gmail.config.email = "operator@example.com"
    mock_gmail.config.imap_server = "imap.example.com"
    mock_gmail.config.smtp_server = "smtp.example.com"
    mock_gmail.check_connection.return_value = {"imap": True, "smtp": True, "errors": {}}

    mock_cal = MagicMock()
    mock_cal.discover_calendars.side_effect = RuntimeError("CalDAV server unavailable")

    tool = CheckConnectionStatusTool()
    res = await tool.execute(mock_gmail, mock_cal, {})
    assert res["caldav_ok"] is False
    assert res["calendars_count"] == 0
    assert "CalDAV server unavailable" in res["details"]["caldav_error"]



@pytest.mark.asyncio
async def test_search_emails_tool() -> None:
    mock_gmail = MagicMock()
    mock_gmail.search_emails.return_value = [
        EmailMessage(id="1", subject="Test", sender="a@b.com", date="today", body="hello")
    ]
    mock_cal = MagicMock()

    tool = SearchEmailsTool()
    res = await tool.execute(mock_gmail, mock_cal, {"query": "hello", "limit": 5})
    assert res["count"] == 1
    assert res["emails"][0]["subject"] == "Test"


@pytest.mark.asyncio
async def test_get_email_tool() -> None:
    mock_gmail = MagicMock()
    mock_gmail.get_email.return_value = EmailMessage(
        id="42", subject="Specific Email", sender="a@b.com", date="today", body="content"
    )
    mock_cal = MagicMock()

    tool = GetEmailTool()
    res = await tool.execute(mock_gmail, mock_cal, {"email_id": "42"})
    assert res["id"] == "42"
    assert res["subject"] == "Specific Email"


@pytest.mark.asyncio
async def test_send_email_tool_requires_confirm() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()

    tool = SendEmailTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(
            mock_gmail,
            mock_cal,
            {"to": ["user@example.com"], "subject": "Test", "body": "Body", "confirm": False},
        )


@pytest.mark.asyncio
async def test_send_email_tool_success() -> None:
    mock_gmail = MagicMock()
    mock_gmail.send_email.return_value = SendEmailResult(
        success=True, action="send_email", to=["user@example.com"], subject="Test"
    )
    mock_cal = MagicMock()

    tool = SendEmailTool()
    res = await tool.execute(
        mock_gmail,
        mock_cal,
        {"to": ["user@example.com"], "subject": "Test", "body": "Body", "confirm": True},
    )
    assert res["success"] is True
    assert res["to"] == ["user@example.com"]


@pytest.mark.asyncio
async def test_list_calendars_tool() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.discover_calendars.return_value = [
        CalendarInfo(name="Primary", href="/primary/", url="https://cal.example.com/primary/")
    ]

    tool = ListCalendarsTool()
    res = await tool.execute(mock_gmail, mock_cal, {})
    assert res["count"] == 1
    assert res["calendars"][0]["name"] == "Primary"


@pytest.mark.asyncio
async def test_list_calendar_events_tool() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.list_events.return_value = [
        CalendarEvent(
            uid="ev-1",
            summary="Planning",
            description="",
            start="20261106T100000Z",
            end="20261106T110000Z",
            location="",
            status="CONFIRMED",
            calendar="Primary",
            href="/ev-1.ics",
        )
    ]

    tool = ListCalendarEventsTool()
    res = await tool.execute(mock_gmail, mock_cal, {"calendar": "Primary", "limit": 10})
    assert res["count"] == 1
    assert res["events"][0]["summary"] == "Planning"


@pytest.mark.asyncio
async def test_create_calendar_event_tool_requires_confirm() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()

    tool = CreateCalendarEventTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(
            mock_gmail,
            mock_cal,
            {
                "summary": "Meeting",
                "start": "20261106T100000Z",
                "end": "20261106T110000Z",
                "confirm": False,
            },
        )


@pytest.mark.asyncio
async def test_create_calendar_event_tool_success() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.create_event.return_value = CreateEventResult(
        success=True,
        action="create",
        calendar="Primary",
        uid="new-uid-123",
        summary="Meeting",
        start="20261106T100000Z",
        end="20261106T110000Z",
        url="https://cal.example.com/new-uid-123.ics",
    )

    tool = CreateCalendarEventTool()
    res = await tool.execute(
        mock_gmail,
        mock_cal,
        {
            "summary": "Meeting",
            "start": "20261106T100000Z",
            "end": "20261106T110000Z",
            "confirm": True,
        },
    )
    assert res["success"] is True
    assert res["uid"] == "new-uid-123"


@pytest.mark.asyncio
async def test_delete_calendar_event_tool_requires_confirm() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()

    tool = DeleteCalendarEventTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "confirm": False})


@pytest.mark.asyncio
async def test_delete_calendar_event_tool_success() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.delete_event.return_value = DeleteEventResult(
        success=True, action="delete", calendar="Primary", uid="ev-123", status=204
    )

    tool = DeleteCalendarEventTool()
    res = await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "confirm": True})
    assert res["success"] is True
    assert res["uid"] == "ev-123"


@pytest.mark.asyncio
async def test_update_calendar_event_tool() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.update_event.return_value = UpdateEventResult(
        success=True,
        action="update",
        calendar="Primary",
        uid="ev-123",
        summary="New Title",
        start="20261106T100000Z",
        end="20261106T110000Z",
        location="New Loc",
        description="New Desc",
        url="https://cal.example.com/ev-123.ics",
    )

    tool = UpdateCalendarEventTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "summary": "New Title", "confirm": False})

    res = await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "summary": "New Title", "confirm": True})
    assert res["success"] is True
    assert res["summary"] == "New Title"


@pytest.mark.asyncio
async def test_move_calendar_event_tool() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.move_event.return_value = MoveEventResult(
        success=True,
        action="move",
        from_calendar="Primary",
        to_calendar="Contract Clients",
        uid="ev-123",
        summary="Title",
        url="https://cal.example.com/ev-123.ics",
    )

    tool = MoveCalendarEventTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "to_calendar": "Contract Clients", "confirm": False})

    res = await tool.execute(mock_gmail, mock_cal, {"uid": "ev-123", "to_calendar": "Contract Clients", "confirm": True})
    assert res["success"] is True
    assert res["to_calendar"] == "Contract Clients"


@pytest.mark.asyncio
async def test_batch_calendar_mutations_tool() -> None:
    mock_gmail = MagicMock()
    mock_cal = MagicMock()
    mock_cal.batch_mutate.return_value = BatchMutationsResult(
        success=True,
        action="batch_mutations",
        total_operations=1,
        succeeded_operations=1,
        failed_operations=0,
        results=[
            BatchMutationItemResult(index=0, action="create", success=True, uid="new-1")
        ],
    )

    tool = BatchCalendarMutationsTool()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        await tool.execute(mock_gmail, mock_cal, {"operations": [{"action": "create"}], "confirm": False})

    res = await tool.execute(mock_gmail, mock_cal, {"operations": [{"action": "create"}], "confirm": True})
    assert res["success"] is True
    assert res["succeeded_operations"] == 1
