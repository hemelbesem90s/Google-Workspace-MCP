"""Calendar tools for Google Workspace MCP (CalDAV multi-calendar discovery, query, and mutations)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional
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
    start_date: Optional[str] = Field(
        None,
        description="Optional start date/datetime filter (e.g. '2026-01-01')",
    )
    end_date: Optional[str] = Field(
        None,
        description="Optional end date/datetime filter (e.g. '2026-12-31')",
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
            start_date=args.start_date,
            end_date=args.end_date,
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
    all_day: Optional[bool] = Field(
        None,
        description="Whether this is an all-day or multi-day date event (defaults to true if dates like YYYY-MM-DD are passed)",
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
            all_day=args.all_day,
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


class UpdateCalendarEventSchema(ToolSchema):
    """Input parameters for update_calendar_event."""

    uid: str = Field(
        ...,
        description="Unique identifier (UID) of the event to update",
        min_length=1,
    )
    summary: Optional[str] = Field(
        None,
        description="Updated event title",
    )
    start: Optional[str] = Field(
        None,
        description="Updated start datetime or date string (e.g., '2026-11-06T14:00:00Z' or '2026-11-06')",
    )
    end: Optional[str] = Field(
        None,
        description="Updated end datetime or date string (e.g., '2026-11-06T15:00:00Z' or '2026-11-07')",
    )
    location: Optional[str] = Field(
        None,
        description="Updated event location or address",
    )
    description: Optional[str] = Field(
        None,
        description="Updated description notes",
    )
    calendar: Optional[str] = Field(
        None,
        description="Calendar name containing the event (if known)",
    )
    all_day: Optional[bool] = Field(
        None,
        description="Whether this is an all-day or multi-day date event",
    )
    notify_attendees: bool = Field(
        False,
        description=(
            "Whether to send email update notifications to attendees. Default is false (silent update). "
            "Set to true only if attendees should receive an email about this modification."
        ),
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to update the calendar event. Must be set to true.",
    )


class UpdateCalendarEventTool(BaseTool):
    """Tool to update an existing calendar event via CalDAV gated by hardware biometrics."""

    name = "update_calendar_event"
    description = (
        "Update an existing calendar event's summary, location, description, or times by UID without recreating it. "
        "Requires explicit confirmation (confirm=true) and is strictly gated by hardware biometrics."
    )
    input_schema = UpdateCalendarEventSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = UpdateCalendarEventSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to update a calendar event.")

        res = await asyncio.to_thread(
            calendar_client.update_event,
            event_uid=args.uid,
            summary=args.summary,
            start=args.start,
            end=args.end,
            location=args.location,
            description=args.description,
            calendar_name=args.calendar,
            all_day=args.all_day,
            notify_attendees=args.notify_attendees,
        )
        return res.model_dump()


class MoveCalendarEventSchema(ToolSchema):
    """Input parameters for move_calendar_event."""

    uid: str = Field(
        ...,
        description="Unique identifier (UID) of the event to move",
        min_length=1,
    )
    to_calendar: str = Field(
        ...,
        description="Target destination calendar name (e.g., 'Contract Clients')",
    )
    from_calendar: Optional[str] = Field(
        None,
        description="Source calendar name containing the event (if known)",
    )
    location: Optional[str] = Field(
        None,
        description="Optional updated location to set when moving the event",
    )
    notify_attendees: bool = Field(
        False,
        description=(
            "Whether to send email update notifications to attendees. Default is false (silent update). "
            "Set to true only if attendees should receive an email about this modification."
        ),
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to move the calendar event. Must be set to true.",
    )


class MoveCalendarEventTool(BaseTool):
    """Tool to move a calendar event between Google calendars gated by hardware biometrics."""

    name = "move_calendar_event"
    description = (
        "Move a calendar event from one calendar to another (e.g., from Primary to Contract Clients) "
        "while optionally updating its location. Requires explicit confirmation (confirm=true) and biometric gating."
    )
    input_schema = MoveCalendarEventSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = MoveCalendarEventSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to move a calendar event.")

        res = await asyncio.to_thread(
            calendar_client.move_event,
            event_uid=args.uid,
            to_calendar=args.to_calendar,
            from_calendar=args.from_calendar,
            new_location=args.location,
            notify_attendees=args.notify_attendees,
        )
        return res.model_dump()


class BatchCalendarMutationsSchema(ToolSchema):
    """Input parameters for batch_calendar_mutations."""

    operations: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "List of mutation operations to perform sequentially. Each operation must include 'action' "
            "('create', 'update', 'delete', or 'move') along with relevant fields (uid, summary, start, end, "
            "calendar, to_calendar, location, description, all_day)."
        ),
        min_length=1,
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to execute the batch mutations. Must be set to true.",
    )


class BatchCalendarMutationsTool(BaseTool):
    """Tool to execute a batch of calendar mutations with a single unified biometric gate."""

    name = "batch_calendar_mutations"
    description = (
        "Execute a batch list of calendar operations (create, update, delete, move) in a single run. "
        "Allows modifying multiple events under a single hardware biometric prompt, avoiding repetitive Touch ID modals. "
        "Requires explicit confirmation (confirm=true)."
    )
    input_schema = BatchCalendarMutationsSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = BatchCalendarMutationsSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to execute batch calendar mutations.")

        res = await asyncio.to_thread(
            calendar_client.batch_mutate,
            operations=args.operations,
        )
        return res.model_dump()
