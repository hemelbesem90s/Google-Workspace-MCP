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


def parse_datetime_filter(dt_str: str) -> tuple[datetime.datetime, bool, bool]:
    """Parse an ISO 8601 or compact iCalendar date/datetime string.

    Supports:
    - Date-only: ``YYYY-MM-DD``, ``YYYYMMDD``
    - Datetime: ``YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]``, ``YYYY-MM-DD HH:MM[:SS][Z|±HH:MM]``,
      ``YYYYMMDDTHHMMSS[Z]``

    Returns:
        tuple of (parsed_datetime_in_utc_if_tz_aware, is_date_only, has_tz)
    """
    clean = dt_str.strip()
    if not clean:
        raise CalendarError("Date/datetime filter cannot be empty.")

    date_match = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", clean)
    if date_match:
        year, month, day = (int(g) for g in date_match.groups())
        try:
            return datetime.datetime(year, month, day), True, False
        except ValueError as exc:
            raise CalendarError(f"Invalid calendar date '{dt_str}': {exc}") from exc

    compact_dt_match = re.fullmatch(
        r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?",
        clean,
        flags=re.IGNORECASE,
    )
    if compact_dt_match:
        year, month, day, hour, minute, second = (int(g) for g in compact_dt_match.groups()[:6])
        z_suffix = compact_dt_match.group(7)
        try:
            dt = datetime.datetime(year, month, day, hour, minute, second)
        except ValueError as exc:
            raise CalendarError(f"Invalid iCalendar datetime '{dt_str}': {exc}") from exc
        if z_suffix:
            return dt.replace(tzinfo=datetime.timezone.utc), False, True
        return dt, False, False

    # Normalize space separator, trailing Z, and ±HHMM offsets for Python 3.10+ fromisoformat()
    normalized = clean.replace(" ", "T")
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)

    try:
        dt = datetime.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CalendarError(f"Invalid date or datetime format: '{dt_str}'") from exc

    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc), False, True
    return dt, False, False


def _normalize_cmp_pair(
    a_dt: datetime.datetime,
    a_has_tz: bool,
    b_dt: datetime.datetime,
    b_has_tz: bool,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return comparable datetimes: UTC-aware if both have tzinfo, otherwise naive wall-clock."""
    if a_has_tz and b_has_tz:
        return a_dt, b_dt
    return a_dt.replace(tzinfo=None), b_dt.replace(tzinfo=None)


def _resolve_filter_bounds(
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[Optional[tuple[datetime.datetime, bool]], Optional[tuple[datetime.datetime, bool]], bool]:
    """Parse start_date and end_date into inclusive boundary tuples and emptiness flag.

    Returns:
        ((start_dt, start_has_tz) | None, (end_dt, end_has_tz) | None, is_inverted_empty)
    """
    start_bound: Optional[tuple[datetime.datetime, bool]] = None
    end_bound: Optional[tuple[datetime.datetime, bool]] = None

    if start_date:
        s_dt, _, s_has_tz = parse_datetime_filter(start_date)
        start_bound = (s_dt, s_has_tz)

    if end_date:
        e_dt, e_date_only, e_has_tz = parse_datetime_filter(end_date)
        if e_date_only:
            e_dt = e_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        end_bound = (e_dt, e_has_tz)

    if start_bound and end_bound:
        s_cmp, e_cmp = _normalize_cmp_pair(
            start_bound[0], start_bound[1], end_bound[0], end_bound[1]
        )
        if s_cmp > e_cmp:
            return start_bound, end_bound, True

    return start_bound, end_bound, False


def _event_matches_date_range(
    event: CalendarEvent,
    start_date: Optional[str],
    end_date: Optional[str],
) -> bool:
    """Return True if an event overlaps the inclusive [start_date, end_date] filter window."""
    if not start_date and not end_date:
        return True

    start_bound, end_bound, is_inverted = _resolve_filter_bounds(start_date, end_date)
    if is_inverted:
        return False

    if not event.start:
        return False
    try:
        ev_start_dt, ev_start_date_only, ev_start_has_tz = parse_datetime_filter(event.start)
    except CalendarError:
        return False

    ev_end_dt: Optional[datetime.datetime] = None
    ev_end_has_tz = ev_start_has_tz
    if event.end:
        try:
            ev_end_dt, _, ev_end_has_tz = parse_datetime_filter(event.end)
        except CalendarError:
            ev_end_dt = None

    if ev_end_dt is None and ev_start_date_only:
        ev_end_dt = ev_start_dt + datetime.timedelta(days=1)

    # Determine whether this event has a positive duration [ev_start, ev_end) or is a point event
    is_interval = False
    if ev_end_dt is not None:
        es_cmp, ee_cmp = _normalize_cmp_pair(ev_start_dt, ev_start_has_tz, ev_end_dt, ev_end_has_tz)
        is_interval = ee_cmp > es_cmp

    if start_bound:
        fs_dt, fs_has_tz = start_bound
        if is_interval and ev_end_dt is not None:
            ee_cmp, fs_cmp = _normalize_cmp_pair(ev_end_dt, ev_end_has_tz, fs_dt, fs_has_tz)
            if ee_cmp <= fs_cmp:
                return False
        else:
            es_cmp, fs_cmp = _normalize_cmp_pair(ev_start_dt, ev_start_has_tz, fs_dt, fs_has_tz)
            if es_cmp < fs_cmp:
                return False

    if end_bound:
        fe_dt, fe_has_tz = end_bound
        es_cmp, fe_cmp = _normalize_cmp_pair(ev_start_dt, ev_start_has_tz, fe_dt, fe_has_tz)
        if es_cmp > fe_cmp:
            return False

    return True


def _build_caldav_time_range(
    start_date: Optional[str],
    end_date: Optional[str],
    expand_recurring: bool,
) -> tuple[str, str, bool]:
    """Build RFC 4791 <C:time-range> and <C:expand> XML clauses using UTC YYYYMMDDTHHMMSSZ bounds."""
    if not start_date and not end_date:
        return "", "", False

    start_bound, end_bound, is_inverted = _resolve_filter_bounds(start_date, end_date)
    if is_inverted:
        return "", "", True

    c_start: Optional[str] = None
    c_end: Optional[str] = None

    if start_bound:
        # Pad backward by 1 day so events in any timezone offset (UTC-12..UTC+14) are retrieved
        s_padded = start_bound[0].replace(tzinfo=None) - datetime.timedelta(days=1)
        c_start = s_padded.strftime("%Y%m%dT%H%M%SZ")

    if end_bound:
        # Pad forward by 1 day (from midnight or end-of-day) so exclusive RFC 4791 end > start
        e_base = end_bound[0].replace(tzinfo=None, microsecond=0)
        e_padded = e_base + datetime.timedelta(days=1)
        c_end = e_padded.strftime("%Y%m%dT%H%M%SZ")

    time_range_xml = ""
    expand_xml = ""
    if c_start and c_end:
        time_range_xml = f'        <C:time-range start="{c_start}" end="{c_end}"/>\n'
        if expand_recurring:
            expand_xml = f'      <C:expand start="{c_start}" end="{c_end}"/>\n'
    elif c_start:
        time_range_xml = f'        <C:time-range start="{c_start}"/>\n'
    elif c_end:
        time_range_xml = f'        <C:time-range end="{c_end}"/>\n'

    return time_range_xml, expand_xml, False


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


def parse_ics_vevents(ics_text: str) -> List[Dict[str, Any]]:
    """Parse all VEVENT blocks from an iCalendar payload.

    Strictly isolates properties at the top level of each VEVENT block,
    ignoring VTIMEZONE and nested subcomponents such as VALARM.
    """
    events: List[Dict[str, Any]] = []
    # Unfold lines (RFC 5545 section 3.1)
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    in_vevent = False
    subcomponent_depth = 0
    current: Dict[str, Any] = {}

    for line in unfolded.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            in_vevent = True
            subcomponent_depth = 0
            current = {
                "uid": "",
                "summary": "",
                "description": "",
                "start": "",
                "end": "",
                "location": "",
                "status": "",
                "availability": "busy",
            }
            continue
        if line == "END:VEVENT":
            if in_vevent:
                events.append(current)
                in_vevent = False
                subcomponent_depth = 0
            continue

        if not in_vevent:
            continue

        if line.upper().startswith("BEGIN:"):
            subcomponent_depth += 1
            continue
        if line.upper().startswith("END:"):
            subcomponent_depth = max(0, subcomponent_depth - 1)
            continue
        if subcomponent_depth > 0 or ":" not in line:
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
            current["uid"] = val
        elif prop == "SUMMARY":
            current["summary"] = val_unescaped
        elif prop == "DESCRIPTION":
            current["description"] = val_unescaped
        elif prop == "LOCATION":
            current["location"] = val_unescaped
        elif prop == "DTSTART":
            current["start"] = val
        elif prop == "DTEND":
            current["end"] = val
        elif prop == "STATUS":
            current["status"] = val
        elif prop == "TRANSP":
            current["availability"] = "free" if val.upper() == "TRANSPARENT" else "busy"

    return events


def parse_ics_vevent(ics_text: str) -> Dict[str, Any]:
    """Parse key attributes from the first VEVENT iCalendar block.

    Ensures only properties within BEGIN:VEVENT and END:VEVENT are captured,
    strictly ignoring VTIMEZONE and nested VALARM subcomponents.
    """
    events = parse_ics_vevents(ics_text)
    if events:
        return events[0]
    return {
        "uid": "",
        "summary": "",
        "description": "",
        "start": "",
        "end": "",
        "location": "",
        "status": "",
        "availability": "busy",
    }


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
        # Determine RFC 4791 time-range filter and expand elements (always UTC YYYYMMDDTHHMMSSZ)
        time_range_xml, expand_xml, is_inverted = _build_caldav_time_range(
            start_date=start_date,
            end_date=end_date,
            expand_recurring=expand_recurring,
        )
        if is_inverted:
            return []

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
                for parsed in parse_ics_vevents(cal_data_elem.text):
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

                    if not _event_matches_date_range(event, start_date, end_date):
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
        if start_date or end_date:
            _, _, is_inverted = _resolve_filter_bounds(start_date, end_date)
            if is_inverted:
                return []

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
            evs = self.query_calendar_events(
                cal.url,
                cal.name,
                query=query,
                start_date=start_date,
                end_date=end_date,
                expand_recurring=True,
            )
            for ev in evs:
                if not _event_matches_date_range(ev, start_date, end_date):
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
