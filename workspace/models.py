"""Pydantic data models for Google Workspace resources."""

from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AttachmentInfo(BaseModel):
    """Metadata for an email attachment."""

    model_config = ConfigDict(extra="ignore")

    filename: str = Field(..., description="Original filename of the attachment")
    size: int = Field(..., description="File size in bytes")
    downloaded_to: Optional[str] = Field(None, description="Local absolute path where the attachment was saved")


class EmailMessage(BaseModel):
    """Representation of an email message."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="IMAP message sequence or UID")
    subject: str = Field("", description="Decoded email subject line")
    sender: str = Field("", description="Decoded sender address and display name")
    date: str = Field("", description="RFC 2822 date string from email header")
    body: str = Field("", description="Extracted plain text body content")
    attachments: List[AttachmentInfo] = Field(default_factory=list, description="List of attached files")


class SendEmailResult(BaseModel):
    """Result of sending an email."""

    model_config = ConfigDict(extra="ignore")

    success: bool = True
    action: str = "send_email"
    to: List[str]
    subject: str
    attachments: List[str] = Field(default_factory=list)


class CalendarInfo(BaseModel):
    """Information about an accessible CalDAV calendar collection."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Human-readable calendar display name")
    href: str = Field(..., description="Relative CalDAV URI path")
    url: str = Field(..., description="Absolute CalDAV HTTPS endpoint")


class CalendarEvent(BaseModel):
    """Representation of a calendar VEVENT."""

    model_config = ConfigDict(extra="ignore")

    uid: str = Field("", description="Unique identifier of the event")
    summary: str = Field("", description="Event title or summary")
    description: str = Field("", description="Event description or notes")
    start: str = Field("", description="Event start timestamp (ISO 8601 or iCal format)")
    end: str = Field("", description="Event end timestamp (ISO 8601 or iCal format)")
    location: str = Field("", description="Event location or conference link")
    status: str = Field("", description="Event confirmation status (e.g., CONFIRMED, TENTATIVE)")
    calendar: str = Field("", description="Name of the calendar containing this event")
    href: str = Field("", description="CalDAV resource href path")


class CreateEventResult(BaseModel):
    """Result of creating a calendar event."""

    model_config = ConfigDict(extra="ignore")

    success: bool = True
    action: str = "create"
    calendar: str
    uid: str
    summary: str
    start: str
    end: str
    url: str


class DeleteEventResult(BaseModel):
    """Result of deleting a calendar event."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    action: str = "delete"
    calendar: Optional[str] = None
    uid: str
    status: Optional[int] = None
    error: Optional[str] = None


class ConnectionStatus(BaseModel):
    """Diagnostic status of Workspace IMAP, SMTP, and CalDAV services."""

    model_config = ConfigDict(extra="ignore")

    email: str = Field(..., description="Configured workspace email address")
    imap_ok: bool = Field(False, description="Whether IMAP connection and login succeeded")
    caldav_ok: bool = Field(False, description="Whether CalDAV connection and discovery succeeded")
    smtp_ok: bool = Field(False, description="Whether SMTP connection and login succeeded")
    calendars_count: int = Field(0, description="Number of discovered calendars")
    calendars: List[str] = Field(default_factory=list, description="Names of discovered calendars")
    details: dict[str, Any] = Field(default_factory=dict, description="Diagnostics details")
