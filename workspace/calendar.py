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
from workspace.models import CalendarEvent, CalendarInfo, CreateEventResult, DeleteEventResult

logger = logging.getLogger(__name__)


def format_ical_datetime(dt_str: str) -> str:
    """Format standard ISO 8601 or date strings into iCalendar DTSTART/DTEND format."""
    clean = re.sub(r"[-:]", "", dt_str).strip().replace(" ", "T")
    if "T" in clean and not clean.endswith("Z"):
        clean += "Z"
    return clean


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

    return event


def generate_ics(
    uid: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
) -> str:
    """Generate an RFC 5545 compliant VCALENDAR/VEVENT payload."""
    dtstamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc_sum = summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    esc_desc = description.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    esc_loc = location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Google Workspace MCP//CalDAV Client//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"DTSTAMP:{dtstamp}",
        f"UID:{uid}",
        f"SUMMARY:{esc_sum}",
        f"DTSTART:{format_ical_datetime(start)}",
        f"DTEND:{format_ical_datetime(end)}",
    ]
    if esc_desc:
        lines.append(f"DESCRIPTION:{esc_desc}")
    if esc_loc:
        lines.append(f"LOCATION:{esc_loc}")
    lines.append("STATUS:CONFIRMED")
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


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
    ) -> List[CalendarEvent]:
        """Query a single calendar collection using RFC 4791 CalDAV REPORT."""
        report_xml = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
            '  <D:prop>\n'
            '    <C:calendar-data />\n'
            '  </D:prop>\n'
            '  <C:filter>\n'
            '    <C:comp-filter name="VCALENDAR">\n'
            '      <C:comp-filter name="VEVENT" />\n'
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
        limit: int = 25,
    ) -> List[CalendarEvent]:
        """List events across specified calendar or all calendars."""
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

        all_events: List[CalendarEvent] = []
        for cal in target_calendars:
            evs = self.query_calendar_events(cal.url, cal.name, query=query)
            all_events.extend(evs)

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

        domain = self.config.email.split("@")[-1] if "@" in self.config.email else "google.com"
        uid = f"{uuid.uuid4()}@{domain}"
        event_url = f"{target_url.rstrip('/')}/{uid}.ics"
        payload = generate_ics(uid, summary, start, end, description, location).encode("utf-8")

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
