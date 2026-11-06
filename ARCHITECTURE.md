# System Architecture: Google Workspace Hybrid MCP Server

## 1. System Map & Component Topology

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Model Context Protocol                          │
│                   (Claude, Antigravity, Cursor, LLM)                   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ JSON-RPC via stdio
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        WorkspaceMCPServer                              │
│                                                                        │
│   ┌───────────────────────────┐    ┌───────────────────────────────┐   │
│   │   Gmail & CalDAV Read     │    │        Biometric Guard        │   │
│   │  (GmailClient, CalClient) │    │     (verify_user_presence)    │   │
│   └─────────────┬─────────────┘    └───────────────┬───────────────┘   │
└─────────────────┼──────────────────────────────────┼───────────────────┘
                  │                                  │
                  │ (Read Operations)                │ (Write Operations)
                  │                                  ▼
                  │                      ┌───────────────────────┐
                  │                      │      Touch ID /       │
                  │                      │     Windows Hello     │
                  │                      └───────────┬───────────┘
                  │                                  │ Approved
                  ▼                                  ▼
      ┌───────────────────────┐          ┌───────────────────────┐
      │   IMAP SSL (Port 993) │          │  SMTP SSL (Port 465)  │
      │   CalDAV PROPFIND     │          │  CalDAV PUT / DELETE  │
      │   CalDAV REPORT       │          │                       │
      └───────────────────────┘          └───────────────────────┘
```

---

## 2. IMAP Non-Destructive Read Architecture (`BODY.PEEK[]`)

Standard IMAP clients fetching full messages via `(RFC822)` or `(BODY[])` automatically set the `\Seen` flag on the mail server. This alters the user's inbox state and marks unread emails as read.

`GmailClient` strictly fetches message payloads using the `(BODY.PEEK[])` attribute:

```python
status, data = mail.fetch(e_id, "(BODY.PEEK[])")
```

This guarantees:
1. **Inbox State Preservation**: Inspecting headers, content, or downloading attachments leaves unread emails marked as **unread** on the server.
2. **Safe Multi-part MIME Traversal**: Multipart payloads recursively parse `text/plain` parts first. When an email contains only `text/html`, the engine strips tags to provide clean, plain text summaries.
3. **Attachment Path Traversal Sanitation**: Filenames are sanitized via `clean_filename()` using `os.path.basename` to prevent directory traversal attacks (such as `../../`).

---

## 3. RFC 4791 CalDAV Multi-Calendar Architecture

Google Calendar provides CalDAV endpoints adhering to RFC 4791. Standard multi-calendar accounts contain:
- Primary calendar (`/calendar/dav/{email}/events/`)
- Secondary calendars (e.g., client projects, organizations, training bodies)
- Subscribed or virtual calendars (e.g., national holidays)

### Discovery (PROPFIND)
The server executes an HTTP `PROPFIND` request with `Depth: 1` on the base user URL. It collects all collections ending with `/events/` and parses `{DAV:}displayname`.

### Querying (CalDAV REPORT)
To query events without downloading entire iCalendar databases, the client dispatches an XML `REPORT` query:

```xml
<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <C:calendar-data />
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT" />
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
```

### VEVENT Isolation
Google CalDAV responses bundle timezone definitions (`VTIMEZONE`) containing `DTSTART` fields that can be decades old. The parser in `parse_ics_vevent()` extracts properties exclusively within `BEGIN:VEVENT` and `END:VEVENT` blocks, completely isolating event dates from timezone definitions.

---

## 4. Biometric Safety Gate Pipeline

All write tools (`send_email`, `create_calendar_event`, `delete_calendar_event`) pass through a multi-stage security pipeline:

```
[Tool Call: write_operation]
           │
           ▼
[Check: confirm == True?] ──── No ───► Return ConfirmationRequired Error
           │
          Yes
           ▼
[Format Human-Readable Prompt] (Target email, subject, attachments, or event dates)
           │
           ▼
[Check: require_biometric?] ── No ───► Execute Write Directly
           │
          Yes
           ▼
[Trigger Native Hardware Modal]
• macOS: Swift LocalAuthentication (Touch ID / Face ID)
  Fallback: osascript approval dialog
• Windows: Windows Hello UserConsentVerifier
           │
           ├─ Approved ──────────────► Execute Write Tool via SMTP / CalDAV
           │
           └─ Denied / Timeout ──────► Abort and Return BiometricDenied Error
```

---

## 5. Zero-Password Keychain Resolution

To prevent leaking credentials in environment variables or configuration files, the server resolves credentials using a tiered fallback:

1. **Explicit Function Arguments**: For unit test injection and isolated sandboxes.
2. **Environment Variables**: For automated CI/CD runners (`WORKSPACE_EMAIL`, `WORKSPACE_APP_PASSWORD`).
3. **Local JSON Configuration**: Discovered at `WORKSPACE_CONFIG_PATH` or standard company locations.
4. **OS Keychain (`keyring`)**:
   - On macOS, when running in a Python virtual environment, macOS Keychain Access Control Lists (ACLs) can prompt interactive GUI dialogs that hang headless automated tasks.
   - The server features a dedicated system bridge (`_load_from_macos_system_keyring`) querying `/usr/bin/python3`, allowing existing authorized ACL credentials to resolve in under 50 milliseconds with zero user friction.

---

## 6. Separation of Concerns: Agentic Skills vs. MCP Core

The MCP server provides the deterministic data and execution layer. A local agentic skill (such as `gmail-reader-searcher` or `calendar-reader-searcher`) sits between the user and the MCP server:

- **MCP Server Core**: Handles RFC compliance, TLS sockets, CalDAV XML serialization, OS Keychain lookup, and biometric hardware enforcement. Contains zero personal data, company templates, or client IDs.
- **Local Agentic Skill**: Handles conversational reasoning, company email signatures, Dutch/English tone, client code lookups (UPID/CID), and user prompt engineering.
