"""Google Workspace MCP tools registry."""

from __future__ import annotations

from typing import Set

from workspace_mcp.tools.base import BaseTool, ToolRegistry, ToolSchema
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

WRITE_TOOL_NAMES: Set[str] = {
    "send_email",
    "create_calendar_event",
    "delete_calendar_event",
    "update_calendar_event",
    "move_calendar_event",
    "batch_calendar_mutations",
}


def register_all_tools(registry: ToolRegistry) -> None:
    """Register all available Google Workspace tools into the registry."""
    # Diagnostics & Health
    registry.register(CheckConnectionStatusTool())

    # Email
    registry.register(SearchEmailsTool())
    registry.register(GetEmailTool())
    registry.register(SendEmailTool())

    # Calendar
    registry.register(ListCalendarsTool())
    registry.register(ListCalendarEventsTool())
    registry.register(CreateCalendarEventTool())
    registry.register(DeleteCalendarEventTool())
    registry.register(UpdateCalendarEventTool())
    registry.register(MoveCalendarEventTool())
    registry.register(BatchCalendarMutationsTool())


__all__ = [
    "BaseTool",
    "ToolRegistry",
    "ToolSchema",
    "WRITE_TOOL_NAMES",
    "register_all_tools",
    "CheckConnectionStatusTool",
    "SearchEmailsTool",
    "GetEmailTool",
    "SendEmailTool",
    "ListCalendarsTool",
    "ListCalendarEventsTool",
    "CreateCalendarEventTool",
    "DeleteCalendarEventTool",
]
