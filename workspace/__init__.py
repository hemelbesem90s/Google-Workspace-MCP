"""Google Workspace SDK providing non-destructive IMAP PEEK and CalDAV clients."""

from __future__ import annotations

from workspace.calendar import CalendarClient, format_ical_datetime, generate_ics, parse_ics_vevent
from workspace.config import WorkspaceConfig, load_workspace_config
from workspace.exceptions import (
    BiometricError,
    BiometricHardwareUnavailable,
    BiometricVerificationDenied,
    CalendarError,
    EventNotFoundError,
    GmailError,
    WorkspaceAuthError,
    WorkspaceError,
)
from workspace.gmail import GmailClient, clean_filename, decode_mime_header
from workspace.models import (
    AttachmentInfo,
    CalendarEvent,
    CalendarInfo,
    ConnectionStatus,
    CreateEventResult,
    DeleteEventResult,
    EmailMessage,
    SendEmailResult,
)

__all__ = [
    "WorkspaceConfig",
    "load_workspace_config",
    "GmailClient",
    "CalendarClient",
    "clean_filename",
    "decode_mime_header",
    "format_ical_datetime",
    "generate_ics",
    "parse_ics_vevent",
    "AttachmentInfo",
    "EmailMessage",
    "SendEmailResult",
    "CalendarInfo",
    "CalendarEvent",
    "CreateEventResult",
    "DeleteEventResult",
    "ConnectionStatus",
    "WorkspaceError",
    "WorkspaceAuthError",
    "GmailError",
    "CalendarError",
    "EventNotFoundError",
    "BiometricError",
    "BiometricVerificationDenied",
    "BiometricHardwareUnavailable",
]

