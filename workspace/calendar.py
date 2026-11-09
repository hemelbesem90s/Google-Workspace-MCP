"""CalDAV client for Google Calendar supporting multi-calendar discovery, querying, and mutations."""

from __future__ import annotations

import base64
import datetime
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from workspace.config import WorkspaceConfig
from workspace.exceptions import CalendarError
from workspace.models import (
    BatchMutationItemResult,
    BatchMutationsResult,
    CalendarEvent,
    CalendarInfo,
    CreateEventResult,
    DeleteEventResult,
    MoveEventResult,
    UpdateEventResult,
)

logger = logging.getLogger(__name__)


def format_ical_datetime(dt_str: str, is_all_day: Optional[bool] = None) -> tuple[str, bool]:
    """Format standard ISO 8601 or date strings into iCalendar DTSTART/DTEND format.

    Returns:
        tuple of (formatted_string, is_all_day_boolean)
    """
    clean = dt_str.strip()
    # Check if purely date: YYYY-MM-DD or YYYYMMDD
    is_date_only = bool(re.match(r"^\d{4}-?\d{2}-?\d{2}$", clean))
    all_day = is_all_day if is_all_day is not None else is_date_only

    clean_digits = re.sub(r"[-:]", "", clean).replace(" ", "T")
    if all_day:
        # Return YYYYMMDD without time or Z
        return clean_digits[:8], True

    if "T" in clean_digits and not clean_digits.endswith("Z"):
        clean_digits += "Z"
    return clean_digits, False


def parse_ics_vevent(ics_text: str) -> Dict[str, Any]:
    """Parse key attributes from a VEVENT iCalendar block.

    Ensures only properties within BEGIN:VEVENT and END:VEVENT are captured,
    strictly ignoring VTIMEZONE subcomponents.
    """
    event: Dict[str, Any] = {
        "uid": "",
        "summary": "",
        "description": "",
        "start": "",
        "end": "",
        "location": "",
        "status": "",
        "availability": "busy",
    }
    # Unfold lines (RFC 5545 section 3.1)
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    in_vevent = False

    for line in unfolded.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            in_vevent = True
            continue
        if line == "END:VEVENT":
            in_vevent = False
            continue

        if not in_vevent or ":" not in line:
            continue

        key_part, val = line.split(":", 1)
        prop = key_part.split(";")[0].upper()

        val_unescaped = (
            val.replace(r"\,", ",")
            .replace(r"\n", "\n")
            .replace(r"\;", ";")
            .replace(r"\\", "\\")
        )

        if prop == "UID":
            event["uid"] = val
        elif prop == "SUMMARY":
            event["summary"] = val_unescaped
        elif prop == "DESCRIPTION":
            event["description"] = val_unescaped
        elif prop == "LOCATION":
            event["location"] = val_unescaped
        elif prop == "DTSTART":
            event["start"] = val
        elif prop == "DTEND":
            event["end"] = val
        elif prop == "STATUS":
            event["status"] = val
        elif prop == "TRANSP":
            event["availability"] = "free" if val.upper() == "TRANSPARENT" else "busy"

    return event


def generate_ics(
    uid: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    all_day: Optional[bool] = None,
    availability: str = "busy",
) -> str:
    """Generate an RFC 5545 compliant VCALENDAR/VEVENT payload supporting timed and multi-day date events."""
    dtstamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc_sum = summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    esc_desc = description.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    esc_loc = location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    start_val, start_all_day = format_ical_datetime(start, all_day)
    end_val, end_all_day = format_ical_datetime(end, all_day)

    dtstart_line = f"DTSTART;VALUE=DATE:{start_val}" if start_all_day else f"DTSTART:{start_val}"
    dtend_line = f"DTEND;VALUE=DATE:{end_val}" if end_all_day else f"DTEND:{end_val}"
    transp_line = "TRANSP:TRANSPARENT" if availability.lower() == "free" else "TRANSP:OPAQUE"

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Google Workspace MCP//CalDAV Client//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"DTSTAMP:{dtstamp}",
        f"UID:{uid}",
        f"SUMMARY:{esc_sum}",
        dtstart_line,
        dtend_line,
    ]
    if esc_desc:
        lines.append(f"DESCRIPTION:{esc_desc}")
    if esc_loc:
        lines.append(f"LOCATION:{esc_loc}")
    lines.append("STATUS:CONFIRMED")
    lines.append(transp_line)
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def suppress_attendee_scheduling(ics_text: str) -> str:
    """Inject SCHEDULE-AGENT=NONE into all ATTENDEE lines to prevent CalDAV servers

    (such as Google Calendar) from automatically dispatching update notification
    emails to attendees when the event is mutated via PUT.
    """
    lines: List[str] = []
    for line in ics_text.splitlines():
        if line.startswith("ATTENDEE;") or line.startswith("ATTENDEE:"):
            if "SCHEDULE-AGENT=" not in line:
                if line.startswith("ATTENDEE;"):
                    line = "ATTENDEE;SCHEDULE-AGENT=NONE;" + line[9:]
                else:
                    line = "ATTENDEE;SCHEDULE-AGENT=NONE:" + line[9:]
            else:
                line = re.sub(r"SCHEDULE-AGENT=[A-Z-]+", "SCHEDULE-AGENT=NONE", line)
        lines.append(line)
    return "\r\n".join(lines) + ("\r\n" if ics_text.endswith("\r\n") or ics_text.endswith("\n") else "")


class CalendarClient:
    """CalDAV client for Google Calendar multi-calendar operations."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config

    def _build_request(
        self,
        method: str,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> urllib.request.Request:
        req = urllib.request.Request(url, data=data, method=method)
        auth = base64.b64encode(f"{self.config.email}:{self.config.app_password}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("User-Agent", "Google-Workspace-MCP/1.0")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        return req

    def discover_calendars(self) -> List[CalendarInfo]:
        """Discover all accessible calendars (primary, secondary, and team calendars)."""
        url = self.config.get_caldav_url()
        req = self._build_request("PROPFIND", url, headers={"Depth": "1"})

        calendars: List[CalendarInfo] = []
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                data = resp.read()
            root = ET.fromstring(data)
            for resp_node in root.findall(".//{DAV:}response"):
                href_elem = resp_node.find("{DAV:}href")
                href: str = (href_elem.text or "") if href_elem is not None else ""
                if not href.endswith("/events/"):
                    continue

                displayname_elem = resp_node.find(".//{DAV:}displayname")
                displayname = displayname_elem.text if displayname_elem is not None and displayname_elem.text else ""
                if not displayname:
                    displayname = "Primary" if self.config.email in href else Path(href.rstrip("/")).parent.name

                full_url = f"https://calendar.google.com{href}" if href.startswith("/") else href
                calendars.append(CalendarInfo(name=displayname, href=href, url=full_url))
        except Exception as exc:
            logger.debug("PROPFIND calendar discovery error, falling back to default primary: %s", exc)
            fallback_href = f"/calendar/dav/{urllib.parse.quote(self.config.email)}/events/"
            calendars.append(
                CalendarInfo(
                    name="Primary",
                    href=fallback_href,
                    url=f"https://calendar.google.com{fallback_href}",
                )
            )

        return calendars

    def query_calendar_events(
        self,
        calendar_url: str,
        calendar_name: str,
        query: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        expand_recurring: bool = True,
    ) -> List[CalendarEvent]:
        """Query a single calendar collection using RFC 4791 CalDAV REPORT.

        If expand_recurring is True and date range is provided, uses <C:expand>
        to expand all recurring events (RRULE) into individual occurrences.
        """
        # Determine time-range filter and expand elements
        time_range_xml = ""
        expand_xml = ""

        if start_date and end_date and expand_recurring:
            c_start, _ = format_ical_datetime(start_date)
            c_end, _ = format_ical_datetime(end_date)
            expand_xml = f'      <C:expand start="{c_start}" end="{c_end}"/>\n'
            time_range_xml = f'        <C:time-range start="{c_start}" end="{c_end}"/>\n'

        report_xml = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
            '  <D:prop>\n'
            '    <C:calendar-data>\n'
            f'{expand_xml}'
            '    </C:calendar-data>\n'
            '  </D:prop>\n'
            '  <C:filter>\n'
            '    <C:comp-filter name="VCALENDAR">\n'
            '      <C:comp-filter name="VEVENT">\n'
            f'{time_range_xml}'
            '      </C:comp-filter>\n'
            '    </C:comp-filter>\n'
            '  </C:filter>\n'
            '</C:calendar-query>'
        ).encode("utf-8")

        req = self._build_request(
            "REPORT",
            calendar_url,
            data=report_xml,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw_xml = resp.read()
        except Exception as exc:
            logger.debug("Failed CalDAV REPORT on %s: %s", calendar_url, exc)
            return []

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            logger.debug("XML parse error on CalDAV REPORT: %s", exc)
            return []

        events: List[CalendarEvent] = []
        for resp_node in root.findall(".//{DAV:}response"):
            href_elem = resp_node.find("{DAV:}href")
            href: str = (href_elem.text or "") if href_elem is not None else ""
            cal_data_elem = resp_node.find(".//{urn:ietf:params:xml:ns:caldav}calendar-data")

            if cal_data_elem is not None and cal_data_elem.text:
                parsed = parse_ics_vevent(cal_data_elem.text)
                uid = parsed["uid"]
                if not uid and href:
                    uid = Path(href).stem

                event = CalendarEvent(
                    uid=uid,
                    summary=parsed["summary"],
                    description=parsed["description"],
                    start=parsed["start"],
                    end=parsed["end"],
                    location=parsed["location"],
                    status=parsed["status"],
                    availability=parsed.get("availability", "busy"),
                    calendar=calendar_name,
                    href=href,
                )

                if query:
                    q_lower = query.lower()
                    matches = (
                        q_lower in event.summary.lower()
                        or q_lower in event.description.lower()
                        or q_lower in event.location.lower()
                        or q_lower in event.calendar.lower()
                    )
                    if not matches:
                        continue

                events.append(event)

        return events

    def list_events(
        self,
        calendar_filter: Optional[str] = None,
        query: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 25,
    ) -> List[CalendarEvent]:
        """List events across specified calendar or all calendars with optional text and date filters."""
        available_calendars = self.discover_calendars()
        target_calendars: List[CalendarInfo] = []

        if calendar_filter:
            cal_lower = calendar_filter.strip().lower()
            if cal_lower == "all":
                target_calendars = available_calendars
            else:
                for c in available_calendars:
                    if cal_lower in c.name.lower() or cal_lower in c.href.lower():
                        target_calendars.append(c)
                if not target_calendars:
                    target_calendars = available_calendars
        else:
            # Default to primary calendar
            for c in available_calendars:
                if self.config.email.lower() in c.href.lower() or "primary" in c.name.lower():
                    target_calendars.append(c)
                    break
            if not target_calendars and available_calendars:
                target_calendars = [available_calendars[0]]

        clean_start_filter = re.sub(r"[-:]", "", start_date).replace(" ", "T")[:8] if start_date else None
        clean_end_filter = re.sub(r"[-:]", "", end_date).replace(" ", "T")[:8] if end_date else None

        all_events: List[CalendarEvent] = []
        for cal in target_calendars:
            evs = self.query_calendar_events(
                cal.url,
                cal.name,
                query=query,
                start_date=start_date,
                end_date=end_date,
                expand_recurring=True,
            )
            for ev in evs:
                ev_date = re.sub(r"[-:]", "", ev.start)[:8]
                if clean_start_filter and ev_date < clean_start_filter:
                    continue
                if clean_end_filter and ev_date > clean_end_filter:
                    continue
                all_events.append(ev)

        all_events.sort(key=lambda ev: ev.start, reverse=True)
        return all_events[:limit]

    def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        calendar_name: Optional[str] = None,
        all_day: Optional[bool] = None,
        availability: str = "busy",
    ) -> CreateEventResult:
        """Create a new calendar event via CalDAV PUT."""
        if not summary:
            raise CalendarError("Event summary/title is required.")
        if not start or not end:
            raise CalendarError("Event start and end datetimes are required.")

        calendars = self.discover_calendars()
        target_url: Optional[str] = None
        chosen_name = "Primary"

        if calendar_name:
            cn_lower = calendar_name.lower()
            for c in calendars:
                if cn_lower in c.name.lower():
                    target_url = c.url
                    chosen_name = c.name
                    break

        if not target_url:
            for c in calendars:
                if self.config.email.lower() in c.href.lower() or "primary" in c.name.lower():
                    target_url = c.url
                    chosen_name = c.name
                    break
            if not target_url and calendars:
                target_url = calendars[0].url
                chosen_name = calendars[0].name

        if not target_url:
            raise CalendarError("No accessible calendar endpoint found.")

        clean_avail = "free" if availability.lower() == "free" else "busy"
        domain = self.config.email.split("@")[-1] if "@" in self.config.email else "google.com"
        uid = f"{uuid.uuid4()}@{domain}"
        event_url = f"{target_url.rstrip('/')}/{uid}.ics"
        payload = generate_ics(
            uid=uid,
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            all_day=all_day,
            availability=clean_avail,
        ).encode("utf-8")

        req = self._build_request(
            "PUT",
            event_url,
            data=payload,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                if resp.status in (200, 201, 204):
                    return CreateEventResult(
                        success=True,
                        action="create",
                        calendar=chosen_name,
                        uid=uid,
                        summary=summary,
                        start=start,
                        end=end,
                        availability=clean_avail,
                        url=event_url,
                    )
                raise CalendarError(f"Calendar server responded with HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise CalendarError(f"Failed to create event (HTTP {exc.code}): {exc.reason}") from exc
        except Exception as exc:
            if isinstance(exc, CalendarError):
                raise
            raise CalendarError(f"Error creating calendar event: {exc}") from exc

    def delete_event(
        self,
        event_uid: str,
        calendar_name: Optional[str] = None,
    ) -> DeleteEventResult:
        """Delete a calendar event by UID via CalDAV DELETE."""
        if not event_uid:
            raise CalendarError("Event UID is required for deletion.")

        clean_uid = event_uid if event_uid.endswith(".ics") else f"{event_uid}.ics"
        calendars = self.discover_calendars()
        target_calendars = calendars
        if calendar_name:
            target_calendars = [c for c in calendars if calendar_name.lower() in c.name.lower()]

        for cal in target_calendars:
            event_url = f"{cal.url.rstrip('/')}/{clean_uid}"
            req = self._build_request("DELETE", event_url)
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    return DeleteEventResult(
                        success=True,
                        action="delete",
                        calendar=cal.name,
                        uid=event_uid,
                        status=resp.status,
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise CalendarError(f"HTTP error deleting event (HTTP {exc.code}): {exc.reason}") from exc
            except Exception as exc:
                raise CalendarError(f"Error deleting event: {exc}") from exc

        return DeleteEventResult(
            success=False,
            action="delete",
            uid=event_uid,
            error=f"Event UID '{event_uid}' not found in accessible calendars.",
        )

    def get_event_raw(self, event_uid: str, calendar_name: Optional[str] = None) -> tuple[Optional[str], Optional[str], Optional[CalendarInfo]]:
        """Fetch raw ICS content and URL for an event by UID.

        Returns:
            (raw_ics, event_url, calendar_info)
        """
        clean_uid = event_uid if event_uid.endswith(".ics") else f"{event_uid}.ics"
        calendars = self.discover_calendars()
        target_calendars = calendars
        if calendar_name:
            target_calendars = [c for c in calendars if calendar_name.lower() in c.name.lower()]

        for cal in target_calendars:
            event_url = f"{cal.url.rstrip('/')}/{clean_uid}"
            req = self._build_request("GET", event_url)
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    if resp.status == 200:
                        raw = resp.read().decode("utf-8")
                        return raw, event_url, cal
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise CalendarError(f"HTTP error fetching event (HTTP {exc.code}): {exc.reason}") from exc
            except Exception as exc:
                raise CalendarError(f"Error fetching event: {exc}") from exc

        return None, None, None

    def update_event(
        self,
        event_uid: str,
        summary: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        calendar_name: Optional[str] = None,
        all_day: Optional[bool] = None,
        availability: Optional[str] = None,
        notify_attendees: bool = False,
    ) -> UpdateEventResult:
        """Update an existing calendar event's metadata without changing its UID.

        If notify_attendees is False (default), injects SCHEDULE-AGENT=NONE to
        suppress automatic email invitations/updates to attendees.
        """
        if not event_uid:
            raise CalendarError("Event UID is required for update.")

        raw_ics, event_url, cal_info = self.get_event_raw(event_uid, calendar_name=calendar_name)
        if not raw_ics or not event_url or not cal_info:
            raise CalendarError(f"Event with UID '{event_uid}' not found.")

        updated_ics = raw_ics

        if not notify_attendees:
            updated_ics = suppress_attendee_scheduling(updated_ics)

        if summary is not None:
            esc = summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
            if re.search(r"^SUMMARY:.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^SUMMARY:.*$", f"SUMMARY:{esc}", updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"SUMMARY:{esc}\r\nEND:VEVENT")

        if location is not None:
            esc = location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
            if re.search(r"^LOCATION:.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^LOCATION:.*$", f"LOCATION:{esc}", updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"LOCATION:{esc}\r\nEND:VEVENT")

        if description is not None:
            esc = description.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
            if re.search(r"^DESCRIPTION:.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^DESCRIPTION:.*$", f"DESCRIPTION:{esc}", updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"DESCRIPTION:{esc}\r\nEND:VEVENT")

        if start is not None:
            start_val, s_all_day = format_ical_datetime(start, all_day)
            start_line = f"DTSTART;VALUE=DATE:{start_val}" if s_all_day else f"DTSTART:{start_val}"
            if re.search(r"^DTSTART.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^DTSTART.*$", start_line, updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"{start_line}\r\nEND:VEVENT")

        if end is not None:
            end_val, e_all_day = format_ical_datetime(end, all_day)
            end_line = f"DTEND;VALUE=DATE:{end_val}" if e_all_day else f"DTEND:{end_val}"
            if re.search(r"^DTEND.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^DTEND.*$", end_line, updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"{end_line}\r\nEND:VEVENT")

        if availability is not None:
            transp_val = "TRANSPARENT" if availability.lower() == "free" else "OPAQUE"
            transp_line = f"TRANSP:{transp_val}"
            if re.search(r"^TRANSP:.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^TRANSP:.*$", transp_line, updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"{transp_line}\r\nEND:VEVENT")

        req = self._build_request(
            "PUT",
            event_url,
            data=updated_ics.encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                if resp.status in (200, 201, 204):
                    parsed = parse_ics_vevent(updated_ics)
                    return UpdateEventResult(
                        success=True,
                        action="update",
                        calendar=cal_info.name,
                        uid=event_uid,
                        summary=parsed.get("summary") or (summary or ""),
                        start=parsed.get("start") or (start or ""),
                        end=parsed.get("end") or (end or ""),
                        location=parsed.get("location") or (location or ""),
                        description=parsed.get("description") or (description or ""),
                        availability=parsed.get("availability", "busy"),
                        url=event_url,
                    )
                raise CalendarError(f"Calendar server responded with HTTP {resp.status} on update.")
        except urllib.error.HTTPError as exc:
            raise CalendarError(f"Failed to update event (HTTP {exc.code}): {exc.reason}") from exc
        except Exception as exc:
            if isinstance(exc, CalendarError):
                raise
            raise CalendarError(f"Error updating calendar event: {exc}") from exc

    def move_event(
        self,
        event_uid: str,
        to_calendar: str,
        from_calendar: Optional[str] = None,
        new_location: Optional[str] = None,
        notify_attendees: bool = False,
    ) -> MoveEventResult:
        """Move an event from one calendar to another while preserving or updating attributes.

        If notify_attendees is False (default), injects SCHEDULE-AGENT=NONE to
        suppress automatic email invitations/updates to attendees.
        """
        if not event_uid:
            raise CalendarError("Event UID is required to move.")
        if not to_calendar:
            raise CalendarError("Destination calendar name is required.")

        raw_ics, source_url, source_cal = self.get_event_raw(event_uid, calendar_name=from_calendar)
        if not raw_ics or not source_url or not source_cal:
            raise CalendarError(f"Event with UID '{event_uid}' not found in source calendar.")

        calendars = self.discover_calendars()
        target_cal: Optional[CalendarInfo] = None
        tc_lower = to_calendar.lower()
        for c in calendars:
            if tc_lower in c.name.lower():
                target_cal = c
                break

        if not target_cal:
            raise CalendarError(f"Destination calendar '{to_calendar}' not found.")

        updated_ics = raw_ics

        if not notify_attendees:
            updated_ics = suppress_attendee_scheduling(updated_ics)
        if new_location is not None:
            esc = new_location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
            if re.search(r"^LOCATION:.*$", updated_ics, flags=re.MULTILINE):
                updated_ics = re.sub(r"^LOCATION:.*$", f"LOCATION:{esc}", updated_ics, flags=re.MULTILINE)
            else:
                updated_ics = updated_ics.replace("END:VEVENT", f"LOCATION:{esc}\r\nEND:VEVENT")

        clean_uid = event_uid if event_uid.endswith(".ics") else f"{event_uid}.ics"
        dest_url = f"{target_cal.url.rstrip('/')}/{clean_uid}"

        # 1. PUT into destination calendar
        req_put = self._build_request(
            "PUT",
            dest_url,
            data=updated_ics.encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req_put, timeout=self.config.timeout_seconds) as resp:
                if resp.status not in (200, 201, 204):
                    raise CalendarError(f"Destination calendar rejected event: HTTP {resp.status}")
        except Exception as exc:
            raise CalendarError(f"Failed to copy event to '{target_cal.name}': {exc}") from exc

        # 2. DELETE from source calendar
        req_del = self._build_request("DELETE", source_url)
        try:
            with urllib.request.urlopen(req_del, timeout=self.config.timeout_seconds) as resp:
                if resp.status not in (200, 204):
                    logger.warning(f"Could not delete event from source calendar: HTTP {resp.status}")
        except Exception as exc:
            logger.warning(f"Failed to delete event from source calendar after copying: {exc}")

        parsed = parse_ics_vevent(updated_ics)
        return MoveEventResult(
            success=True,
            action="move",
            from_calendar=source_cal.name,
            to_calendar=target_cal.name,
            uid=event_uid,
            summary=parsed.get("summary", ""),
            url=dest_url,
        )

    def batch_mutate(
        self,
        operations: List[Dict[str, Any]],
    ) -> BatchMutationsResult:
        """Execute a batch list of calendar operations (create, update, delete, move)."""
        if not operations:
            raise CalendarError("At least one operation is required for batch mutation.")

        results: List[BatchMutationItemResult] = []
        succeeded = 0
        failed = 0

        for idx, op in enumerate(operations):
            action = str(op.get("action", "")).lower().strip()
            cal_name = op.get("calendar")
            uid = op.get("uid")

            try:
                if action == "create":
                    res = self.create_event(
                        summary=op["summary"],
                        start=op["start"],
                        end=op["end"],
                        description=op.get("description", ""),
                        location=op.get("location", ""),
                        calendar_name=cal_name,
                        all_day=op.get("all_day"),
                        availability=op.get("availability", "busy"),
                    )
                    results.append(
                        BatchMutationItemResult(
                            index=idx,
                            action=action,
                            success=True,
                            uid=res.uid,
                            calendar=res.calendar,
                        )
                    )
                    succeeded += 1

                elif action == "update":
                    if not uid:
                        raise CalendarError("Missing 'uid' for update operation.")
                    u_res = self.update_event(
                        event_uid=uid,
                        summary=op.get("summary"),
                        start=op.get("start"),
                        end=op.get("end"),
                        location=op.get("location"),
                        description=op.get("description"),
                        calendar_name=cal_name,
                        all_day=op.get("all_day"),
                        availability=op.get("availability"),
                        notify_attendees=bool(op.get("notify_attendees", False)),
                    )
                    results.append(
                        BatchMutationItemResult(
                            index=idx,
                            action=action,
                            success=True,
                            uid=u_res.uid,
                            calendar=u_res.calendar,
                        )
                    )
                    succeeded += 1

                elif action == "delete":
                    if not uid:
                        raise CalendarError("Missing 'uid' for delete operation.")
                    d_res = self.delete_event(
                        event_uid=uid,
                        calendar_name=cal_name,
                    )
                    if d_res.success:
                        results.append(
                            BatchMutationItemResult(
                                index=idx,
                                action=action,
                                success=True,
                                uid=d_res.uid,
                                calendar=d_res.calendar,
                            )
                        )
                        succeeded += 1
                    else:
                        results.append(
                            BatchMutationItemResult(
                                index=idx,
                                action=action,
                                success=False,
                                uid=uid,
                                calendar=cal_name,
                                error=d_res.error or "Event not found",
                            )
                        )
                        failed += 1

                elif action == "move":
                    if not uid:
                        raise CalendarError("Missing 'uid' for move operation.")
                    to_cal = op.get("to_calendar")
                    if not to_cal:
                        raise CalendarError("Missing 'to_calendar' for move operation.")
                    m_res = self.move_event(
                        event_uid=uid,
                        to_calendar=to_cal,
                        from_calendar=cal_name,
                        new_location=op.get("location"),
                        notify_attendees=bool(op.get("notify_attendees", False)),
                    )
                    results.append(
                        BatchMutationItemResult(
                            index=idx,
                            action=action,
                            success=True,
                            uid=m_res.uid,
                            calendar=m_res.to_calendar,
                        )
                    )
                    succeeded += 1

                else:
                    raise CalendarError(f"Unsupported batch action: '{action}'")

            except Exception as exc:
                logger.error(f"Batch item #{idx} ({action}) failed: {exc}")
                results.append(
                    BatchMutationItemResult(
                        index=idx,
                        action=action,
                        success=False,
                        uid=uid,
                        calendar=cal_name,
                        error=str(exc),
                    )
                )
                failed += 1

        return BatchMutationsResult(
            success=(failed == 0),
            total_operations=len(operations),
            succeeded_operations=succeeded,
            failed_operations=failed,
            results=results,
        )
