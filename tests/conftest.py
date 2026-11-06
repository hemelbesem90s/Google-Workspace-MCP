"""Test configuration and fixtures for Google Workspace MCP Server."""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pytest

from workspace.config import WorkspaceConfig


@pytest.fixture
def mock_config() -> WorkspaceConfig:
    """Return a mock WorkspaceConfig for testing."""
    return WorkspaceConfig(
        email="test@example.com",
        app_password="test-app-password",
        sender_name="Test Operator",
        imap_server="imap.example.com",
        imap_port=993,
        smtp_server="smtp.example.com",
        smtp_port=465,
        caldav_base_url="https://calendar.example.com/dav/{email}/",
        keyring_service="test_workspace",
        timeout_seconds=5,
    )


@pytest.fixture
def sample_mime_email() -> bytes:
    """Generate a sample raw RFC 2822 email byte payload."""
    msg = MIMEMultipart()
    msg["Subject"] = "Project Update & Review"
    msg["From"] = "Sender Person <sender@example.com>"
    msg["To"] = "test@example.com"
    msg["Date"] = "Wed, 06 Nov 2026 10:00:00 +0100"
    msg["Message-ID"] = "<test-12345@example.com>"

    body = MIMEText("Hello team,\n\nHere is the weekly progress report.\n\nRegards,\nSender", "plain", "utf-8")
    msg.attach(body)
    return msg.as_bytes()


@pytest.fixture
def sample_propfind_xml() -> bytes:
    """Sample WebDAV PROPFIND response containing primary and secondary calendars."""
    return b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/calendar/dav/test%40example.com/events/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>Primary</D:displayname>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/calendar/dav/team_secondary%40group.calendar.example.com/events/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>Team Calendar</D:displayname>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
"""


@pytest.fixture
def sample_report_xml() -> bytes:
    """Sample CalDAV REPORT XML with VEVENT inside VCALENDAR."""
    return b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/calendar/dav/test%40example.com/events/event-uid-123.ics</D:href>
    <D:propstat>
      <D:prop>
        <C:calendar-data>BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//CalDAV//EN
BEGIN:VTIMEZONE
TZID:Europe/Amsterdam
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:event-uid-123
SUMMARY:Quarterly Planning
DESCRIPTION:Discussion of Q4 roadmap and engineering goals
LOCATION:Conference Room A
DTSTART:20261106T140000Z
DTEND:20261106T150000Z
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
</C:calendar-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
"""
