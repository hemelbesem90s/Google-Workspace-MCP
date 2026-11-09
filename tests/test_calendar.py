"""Tests for Google Calendar CalDAV multi-calendar operations."""

from __future__ import annotations

import io
from unittest.mock import MagicMock
import urllib.error
import pytest

from workspace.calendar import (
    CalendarClient,
    format_ical_datetime,
    generate_ics,
    parse_ics_vevent,
)
from workspace.config import WorkspaceConfig
from workspace.exceptions import CalendarError


def test_format_ical_datetime() -> None:
    dt, all_day = format_ical_datetime("2026-11-06T14:00:00Z")
    assert dt == "20261106T140000Z"
    assert all_day is False

    dt, all_day = format_ical_datetime("2026-11-06 14:00:00")
    assert dt == "20261106T140000Z"
    assert all_day is False

    dt, all_day = format_ical_datetime("20261106T140000Z")
    assert dt == "20261106T140000Z"
    assert all_day is False

    dt, all_day = format_ical_datetime("2026-11-06")
    assert dt == "20261106"
    assert all_day is True


def test_parse_ics_vevent_ignores_vtimezone() -> None:
    ics_text = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VTIMEZONE
TZID:Europe/Amsterdam
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:test-uid-777
SUMMARY:Meeting with\\, Team
DESCRIPTION:Line 1\\nLine 2
LOCATION:Room 101
DTSTART:20261106T100000Z
DTEND:20261106T110000Z
STATUS:CONFIRMED
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR"""

    parsed = parse_ics_vevent(ics_text)
    assert parsed["uid"] == "test-uid-777"
    assert parsed["summary"] == "Meeting with, Team"
    assert parsed["description"] == "Line 1\nLine 2"
    assert parsed["location"] == "Room 101"
    assert parsed["start"] == "20261106T100000Z"
    assert parsed["end"] == "20261106T110000Z"
    assert parsed["status"] == "CONFIRMED"
    assert parsed["availability"] == "free"


def test_generate_ics() -> None:
    ics = generate_ics(
        uid="unique-id-888",
        summary="Review & Strategy",
        start="2026-11-06T13:00:00Z",
        end="2026-11-06T14:00:00Z",
        description="Detailed review",
        location="Virtual",
        availability="free",
    )
    assert "BEGIN:VCALENDAR" in ics
    assert "UID:unique-id-888" in ics
    assert "SUMMARY:Review & Strategy" in ics
    assert "DTSTART:20261106T130000Z" in ics
    assert "DTEND:20261106T140000Z" in ics
    assert "DESCRIPTION:Detailed review" in ics
    assert "LOCATION:Virtual" in ics
    assert "TRANSP:TRANSPARENT" in ics
    assert "END:VCALENDAR" in ics

    ics_busy = generate_ics(
        uid="unique-id-889",
        summary="Busy Event",
        start="2026-11-06T13:00:00Z",
        end="2026-11-06T14:00:00Z",
        availability="busy",
    )
    assert "TRANSP:OPAQUE" in ics_busy


def test_discover_calendars(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_propfind_xml
    mock_resp.__enter__.return_value = mock_resp
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: mock_resp)

    calendars = client.discover_calendars()
    assert len(calendars) == 2
    assert calendars[0].name == "Primary"
    assert calendars[1].name == "Team Calendar"
    assert "test%40example.com" in calendars[0].href


def test_query_calendar_events(
    mock_config: WorkspaceConfig,
    sample_report_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_report_xml
    mock_resp.__enter__.return_value = mock_resp
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: mock_resp)

    # 1. Query without filter
    events = client.query_calendar_events("https://calendar.example.com/events/", "Primary")
    assert len(events) == 1
    assert events[0].uid == "event-uid-123"
    assert events[0].summary == "Quarterly Planning"
    assert events[0].calendar == "Primary"

    # 2. Query with matching filter
    matching = client.query_calendar_events(
        "https://calendar.example.com/events/",
        "Primary",
        query="Roadmap",
    )
    assert len(matching) == 1

    # 3. Query with non-matching filter
    non_matching = client.query_calendar_events(
        "https://calendar.example.com/events/",
        "Primary",
        query="NonexistentTerm",
    )
    assert len(non_matching) == 0


def test_list_events(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    sample_report_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    def mock_urlopen(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if req.get_method() == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
        else:
            mock_resp.read.return_value = sample_report_xml
        return mock_resp

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    # List all
    all_events = client.list_events(calendar_filter="all", limit=10)
    assert len(all_events) == 2  # Discovered across both calendars

    # List primary only
    prim_events = client.list_events(calendar_filter="primary", limit=10)
    assert len(prim_events) == 1


def test_create_event_success(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    put_called = []

    def mock_urlopen(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if req.get_method() == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
            mock_resp.status = 200
        elif req.get_method() == "PUT":
            put_called.append(req)
            mock_resp.status = 201
            mock_resp.read.return_value = b""
        return mock_resp

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    res = client.create_event(
        summary="Architecture Alignment",
        start="2026-11-06T14:00:00Z",
        end="2026-11-06T15:00:00Z",
        description="Review hybrid MCP design",
        location="Google Meet",
        calendar_name="Primary",
    )

    assert res.success is True
    assert res.summary == "Architecture Alignment"
    assert res.calendar == "Primary"
    assert len(put_called) == 1
    assert put_called[0].get_method() == "PUT"
    assert "Architecture Alignment" in put_called[0].data.decode("utf-8")


def test_delete_event_success_and_not_found(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    # 1. Success on DELETE
    def mock_urlopen_success(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if req.get_method() == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
            mock_resp.status = 200
        elif req.get_method() == "DELETE":
            mock_resp.status = 204
            mock_resp.read.return_value = b""
        return mock_resp

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen_success)
    res = client.delete_event("event-12345", calendar_name="Primary")
    assert res.success is True
    assert res.uid == "event-12345"
    assert res.status == 204

    # 2. 404 Not found
    def mock_urlopen_404(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if req.get_method() == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
            mock_resp.status = 200
        elif req.get_method() == "DELETE":
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        return mock_resp

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen_404)
    res404 = client.delete_event("event-nonexistent")
    assert res404.success is False
    assert "not found" in res404.error


def test_calendar_validation_errors(mock_config: WorkspaceConfig) -> None:
    client = CalendarClient(mock_config)

    with pytest.raises(CalendarError, match="summary/title is required"):
        client.create_event(summary="", start="20261106T100000Z", end="20261106T110000Z")

    with pytest.raises(CalendarError, match="start and end datetimes are required"):
        client.create_event(summary="Title", start="", end="20261106T110000Z")

    with pytest.raises(CalendarError, match="UID is required"):
        client.delete_event(event_uid="")


def test_calendar_http_errors(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    def mock_urlopen_500(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if req.get_method() == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
            mock_resp.status = 200
            return mock_resp
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen_500)

    with pytest.raises(CalendarError, match="HTTP 500"):
        client.create_event(summary="Err Event", start="20261106T100000Z", end="20261106T110000Z")

    with pytest.raises(CalendarError, match="HTTP error deleting event"):
        client.delete_event(event_uid="err-uid-123")


def test_suppress_attendee_scheduling() -> None:
    from workspace.calendar import suppress_attendee_scheduling

    sample_ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Meeting\r\n"
        "ATTENDEE;CN=John Doe:mailto:john@example.com\r\n"
        "ATTENDEE:mailto:jane@example.com\r\n"
        "ATTENDEE;SCHEDULE-AGENT=SERVER;CN=Bob:mailto:bob@example.com\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    res = suppress_attendee_scheduling(sample_ics)
    assert "SCHEDULE-AGENT=NONE" in res
    assert "SCHEDULE-AGENT=SERVER" not in res
    assert "mailto:john@example.com" in res
    assert "mailto:jane@example.com" in res
    assert "mailto:bob@example.com" in res


def test_calendar_update_move_and_batch(
    mock_config: WorkspaceConfig,
    sample_propfind_xml: bytes,
    sample_report_xml: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CalendarClient(mock_config)

    raw_sample_ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:event-123@google.com\r\n"
        "SUMMARY:Original Summary\r\n"
        "DTSTART:20261106T100000Z\r\n"
        "DTEND:20261106T110000Z\r\n"
        "LOCATION:Old Office\r\n"
        "DESCRIPTION:Old Notes\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    def mock_urlopen(req, timeout=None):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        method = req.get_method()
        if method == "PROPFIND":
            mock_resp.read.return_value = sample_propfind_xml
            mock_resp.status = 200
        elif method == "REPORT":
            mock_resp.read.return_value = sample_report_xml
            mock_resp.status = 207
        elif method == "GET":
            if "event-123" in req.full_url:
                mock_resp.read.return_value = raw_sample_ics.encode("utf-8")
                mock_resp.status = 200
            else:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        elif method in ("PUT", "DELETE"):
            mock_resp.status = 204
            mock_resp.read.return_value = b""
        return mock_resp

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    # 1. Update event
    up_res = client.update_event(
        event_uid="event-123@google.com",
        summary="Updated Title",
        location="New Location",
        description="Updated Notes",
        start="2026-11-06T12:00:00Z",
        end="2026-11-06T13:00:00Z",
    )
    assert up_res.success is True
    assert up_res.summary == "Updated Title"
    assert up_res.location == "New Location"

    # 2. Move event
    mv_res = client.move_event(
        event_uid="event-123@google.com",
        to_calendar="Team Calendar",
        new_location="Secondary Office",
    )
    assert mv_res.success is True
    assert mv_res.to_calendar == "Team Calendar"

    # 3. Batch mutate
    batch_ops = [
        {
            "action": "create",
            "summary": "Batch Created",
            "start": "2026-11-07",
            "end": "2026-11-08",
            "all_day": True,
        },
        {
            "action": "update",
            "uid": "event-123@google.com",
            "location": "Batch Updated Loc",
        },
        {
            "action": "move",
            "uid": "event-123@google.com",
            "to_calendar": "primary",
        },
        {
            "action": "delete",
            "uid": "event-123@google.com",
        },
        {
            "action": "invalid_op",
            "uid": "x",
        }
    ]
    batch_res = client.batch_mutate(batch_ops)
    assert batch_res.total_operations == 5
    assert batch_res.succeeded_operations == 4
    assert batch_res.failed_operations == 1
    assert batch_res.success is False

    # 4. list_events with date filter
    date_filtered = client.list_events(start_date="2026-01-01", end_date="2026-12-31")
    assert isinstance(date_filtered, list)


