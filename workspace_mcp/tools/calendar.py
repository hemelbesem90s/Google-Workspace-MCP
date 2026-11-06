"""Calendar tools for Google Workspace MCP (CalDAV multi-calendar discovery, query, and mutations)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional
from pydantic import Field

from workspace_mcp.tools.base import BaseTool, ToolSchema

if TYPE_CHECKING:
    from workspace.calendar import CalendarClient
    from workspace.gmail import GmailClient


class ListCalendarsSchema(ToolSchema):
    """Input parameters for list_calendars (none required)."""


class ListCalendarsTool(BaseTool):
    """Tool to discover accessible Google calendars."""

    name = "list_calendars"
    description = (
        "Discover all accessible primary and secondary Google calendars in the account "
        "via CalDAV PROPFIND."
    )
    input_schema = ListCalendarsSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        calendars = await asyncio.to_thread(calendar_client.discover_calendars)
        return {
            "count": len(calendars),
            "calendars": [c.model_dump() for c in calendars],
        }


class ListCalendarEventsSchema(ToolSchema):
    """Input parameters for list_calendar_events."""

    calendar: Optional[str] = Field(
        "primary",
        description="Calendar name to query, or 'all' to search across all calendars (default: 'primary')",
    )
    query: Optional[str] = Field(
        None,
        description="Search text filter across summary, description, and location",
    )
    limit: int = Field(
        25,
        description="Maximum number of events to return (default: 25, max: 100)",
        ge=1,
        le=100,
    )


class ListCalendarEventsTool(BaseTool):
    """Tool to list and search calendar events via CalDAV REPORT."""

    name = "list_calendar_events"
    description = (
        "List or search calendar events across a specific calendar or all calendars. "
        "Read-only operation without biometric prompt."
    )
    input_schema = ListCalendarEventsSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = ListCalendarEventsSchema.model_validate(arguments)
        events = await asyncio.to_thread(
            calendar_client.list_events,
            calendar_filter=args.calendar,
            query=args.query,
            limit=args.limit,
        )
        return {
            "count": len(events),
            "events": [e.model_dump() for e in events],
        }


class CreateCalendarEventSchema(ToolSchema):
    """Input parameters for create_calendar_event."""

    summary: str = Field(
        ...,
        description="Event title or summary",
        min_length=1,
    )
    start: str = Field(
        ...,
        description="Start datetime string (e.g., '2026-11-06T14:00:00Z' or '20261106T140000Z')",
        min_length=8,
    )
    end: str = Field(
        ...,
        description="End datetime string (e.g., '2026-11-06T15:00:00Z' or '20261106T150000Z')",
        min_length=8,
    )
    calendar: Optional[str] = Field(
        "primary",
        description="Target calendar name (default: 'primary')",
    )
    description: Optional[str] = Field(
        "",
        description="Event description notes or agenda",
    )
    location: Optional[str] = Field(
        "",
        description="Meeting location or video link",
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to create the calendar event. Must be set to true.",
    )


class CreateCalendarEventTool(BaseTool):
    """Tool to create a calendar event via CalDAV strictly gated by hardware biometrics."""

    name = "create_calendar_event"
    description = (
        "Create an event on Google Calendar via CalDAV. Requires explicit confirmation (confirm=true) "
        "and is strictly gated by native hardware biometric verification (Touch ID / Windows Hello)."
    )
    input_schema = CreateCalendarEventSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = CreateCalendarEventSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to create a calendar event.")

        res = await asyncio.to_thread(
            calendar_client.create_event,
            summary=args.summary,
            start=args.start,
            end=args.end,
            description=args.description or "",
            location=args.location or "",
            calendar_name=args.calendar,
        )
        return res.model_dump()


class DeleteCalendarEventSchema(ToolSchema):
    """Input parameters for delete_calendar_event."""

    uid: str = Field(
        ...,
        description="Unique identifier (UID) of the event to delete",
        min_length=1,
    )
    calendar: Optional[str] = Field(
        None,
        description="Name of the calendar containing the event (if known)",
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to delete the calendar event. Must be set to true.",
    )


class DeleteCalendarEventTool(BaseTool):
    """Tool to delete a calendar event via CalDAV strictly gated by hardware biometrics."""

    name = "delete_calendar_event"
    description = (
        "Delete an event from Google Calendar by UID. Requires explicit confirmation (confirm=true) "
        "and is strictly gated by native hardware biometric verification (Touch ID / Windows Hello)."
    )
    input_schema = DeleteCalendarEventSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = DeleteCalendarEventSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to delete a calendar event.")

        res = await asyncio.to_thread(
            calendar_client.delete_event,
            event_uid=args.uid,
            calendar_name=args.calendar,
        )
        return res.model_dump()
