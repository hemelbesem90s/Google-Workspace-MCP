"""Diagnostic and connectivity status tool for Google Workspace MCP."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict

from workspace_mcp.tools.base import BaseTool, ToolSchema

if TYPE_CHECKING:
    from workspace.calendar import CalendarClient
    from workspace.gmail import GmailClient


class CheckConnectionStatusSchema(ToolSchema):
    """Input parameters for check_connection_status (none required)."""


class CheckConnectionStatusTool(BaseTool):
    """Tool to verify IMAP, SMTP, and CalDAV credentials and connection health."""

    name = "check_connection_status"
    description = (
        "Check connection and authentication status for Google Workspace (IMAP email, "
        "SMTP sending, and CalDAV calendar). Does not mutate any data."
    )
    input_schema = CheckConnectionStatusSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        email_status = await asyncio.to_thread(gmail_client.check_connection)
        try:
            calendars = await asyncio.to_thread(calendar_client.discover_calendars)
            cal_names = [c.name for c in calendars]
            caldav_ok = True
            cal_error = None
        except Exception as exc:
            cal_names = []
            caldav_ok = False
            cal_error = str(exc)

        return {
            "email": gmail_client.config.email,
            "imap_ok": email_status.get("imap", False),
            "smtp_ok": email_status.get("smtp", False),
            "caldav_ok": caldav_ok,
            "calendars_count": len(cal_names),
            "calendars": cal_names,
            "details": {
                "email_errors": email_status.get("errors", {}),
                "caldav_error": cal_error,
                "imap_server": gmail_client.config.imap_server,
                "smtp_server": gmail_client.config.smtp_server,
            },
        }
