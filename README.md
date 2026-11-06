# Google Workspace Hybrid MCP Server

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type Checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)

High-performance, vendor-neutral Model Context Protocol (MCP) server for Google Workspace. It combines non-destructive IMAP email inspection and CalDAV multi-calendar querying with native operating system biometric gates (Touch ID on macOS and Windows Hello on Windows).

---

## Architecture Overview

The system separates the low-level MCP communication engine from higher-level agent reasoning:

```
┌────────────────────────────────────────────────────────┐
│   Agentic AI Client (Antigravity / Claude / Cursor)    │
└───────────────────────────┬────────────────────────────┘
                            │
               Stdio MCP Protocol (JSON-RPC)
                            │
┌───────────────────────────▼────────────────────────────┐
│          Google Workspace Hybrid MCP Server            │
│  (Neutral Protocol Engine, Zero-Password Keychain)    │
├───────────────────────────┬────────────────────────────┤
│     Read Operations       │      Write Operations      │
│  • IMAP PEEK (BODY.PEEK)  │  • SMTP SSL Email Dispatch │
│  • CalDAV Multi-Calendar  │  • CalDAV Event Creation   │
│  • Zero state mutation    │  • CalDAV Event Deletion   │
│  • No read status changes │  • Biometric Presence Gate │
└─────────────┬─────────────┴─────────────┬──────────────┘
              │                           │
              │         Hardware Prompt   │
              │    (Touch ID / Windows Hello)
              ▼                           ▼
┌───────────────────────────┐ ┌──────────────────────────┐
│ Google Workspace Endpoints│ │ Native Hardware Security │
│ • imap.gmail.com:993      │ │ • macOS LAContext        │
│ • smtp.gmail.com:465      │ │ • Windows Hello          │
│ • calendar.google.com/dav │ │ • LocalAuthentication    │
└───────────────────────────┘ └──────────────────────────┘
```

### Core Security Principles
1. **Zero-Password Leakage**: Credentials load securely from the OS Keychain or environment variables. Passwords never appear in logs, error payloads, or tool schemas.
2. **Non-Destructive Reads (`BODY.PEEK[]`)**: IMAP queries always use `BODY.PEEK[]`. Reading or searching emails never marks them as seen or read on the server.
3. **Hardware Biometric Presence Gate**: All state mutations (`send_email`, `create_calendar_event`, `delete_calendar_event`) require explicit user confirmation (`confirm=true`) and trigger a native hardware biometric prompt before connecting.
4. **RFC 4791 CalDAV Multi-Calendar Architecture**: Automatically discovers primary and secondary calendars (such as client or organization calendars). Queries use standard WebDAV `REPORT` XML with `VCALENDAR`/`VEVENT` filtering.

---

## Separation of Concerns: Agentic Skills vs. MCP Core

This MCP server acts as a **strictly neutral and generic execution engine**. It contains no hardcoded user identities, email addresses, personal signatures, or proprietary customer business logic.

To achieve tailored workflows, agents use a **local agentic skill** (such as `gmail-reader-searcher` or `calendar-reader-searcher`) on top of this server:

| Responsibility | MCP Server Core (`workspace-mcp`) | Local Agentic Skill (Workspace Layer) |
| :--- | :--- | :--- |
| **Data Access** | Connects to IMAP, SMTP, and CalDAV | Prompts the user and interprets queries |
| **Credential Storage** | Resolves credentials from OS Keychain | Refers to configured account profile |
| **Safety Gate** | Enforces Touch ID / Windows Hello presence | Obtains user intent and parameters |
| **Email Templates** | Generic MIME constructor | Domain-specific Dutch/English wording |
| **Signatures & Brand** | Neutral sender formatting | Specific company branding and disclaimers |
| **Client Code Lookups** | None (parameter-agnostic) | Project ID mapping (UPID/CID routing) |

---

## Available MCP Tools

The server registers 8 tools across diagnostics, email, and calendar management:

| Tool Name | Type | Biometric Gate | Description |
| :--- | :---: | :---: | :--- |
| `check_connection_status` | Read | No | Tests IMAP, SMTP, and CalDAV credentials and connection health. |
| `search_emails` | Read | No | Searches inbox by query, subject, sender, or unread status (`BODY.PEEK[]`). |
| `get_email` | Read | No | Retrieves full email metadata, body, and attachments by ID. |
| `send_email` | Write | **Yes** | Sends email via SMTP SSL. Requires `confirm=true` and Touch ID. |
| `list_calendars` | Read | No | Discovers accessible primary, secondary, and team calendars. |
| `list_calendar_events` | Read | No | Queries calendar events with text search and calendar filtering. |
| `create_calendar_event` | Write | **Yes** | Creates a new calendar event. Requires `confirm=true` and Touch ID. |
| `delete_calendar_event` | Write | **Yes** | Deletes a calendar event by UID. Requires `confirm=true` and Touch ID. |

---

## Configuration & Environment Variables

The server resolves credentials in the following order:
1. Environment variables.
2. Configuration file path (`WORKSPACE_CONFIG_PATH` or `config.json`).
3. OS Keychain (macOS Keychain service `grobler_gmail` or custom service name).

### Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `WORKSPACE_EMAIL` | *(None)* | Workspace email address. |
| `WORKSPACE_APP_PASSWORD` | *(None)* | App-specific password for IMAP/SMTP/CalDAV. |
| `WORKSPACE_SENDER_NAME` | *(None)* | Display name for outgoing emails. |
| `WORKSPACE_KEYRING_SERVICE` | `grobler_gmail` | OS Keychain service name. |
| `WORKSPACE_IMAP_SERVER` | `imap.gmail.com` | IMAP server hostname. |
| `WORKSPACE_IMAP_PORT` | `993` | IMAP SSL port. |
| `WORKSPACE_SMTP_SERVER` | `smtp.gmail.com` | SMTP server hostname. |
| `WORKSPACE_SMTP_PORT` | `465` | SMTP SSL port. |
| `WORKSPACE_CALDAV_URL` | `https://calendar.google.com/calendar/dav/{email}/` | CalDAV discovery URL. |
| `WORKSPACE_MCP_ENABLE_WRITE_TOOLS` | `true` | Set to `false` to disable all write operations. |
| `WORKSPACE_MCP_REQUIRE_BIOMETRIC` | `true` | Set to `false` in automated CI/CD environments. |
| `WORKSPACE_MCP_LOG_LEVEL` | `INFO` | Server logging level. |

---

## Installation & Setup

### 1. Requirements
- Python 3.10+ (Python 3.13 recommended)
- macOS (Touch ID) or Windows (Windows Hello)
- Google account with an **App Password** configured

### 2. Virtual Environment Setup
```bash
# Clone the repository
git clone https://github.com/hemelbesem90s/Google-Workspace-MCP.git
cd Google-Workspace-MCP

# Create virtual environment and install dependencies
uv venv --python 3.13
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 3. Store Credentials in OS Keychain
You can store your App Password in the macOS Keychain without touching plain-text files:
```bash
# Store email and app password
python3 -c "import keyring; keyring.set_password('grobler_gmail', 'email', 'user@example.com')"
python3 -c "import keyring; keyring.set_password('grobler_gmail', 'app_password', 'xxxx-xxxx-xxxx-xxxx')"
```

### 4. Verify Connection
```bash
workspace-mcp --test-connection
```

---

## MCP Registration

Add the server to your MCP configuration file (e.g. `~/.gemini/config/mcp_config.json` or Claude Desktop config):

```json
{
  "mcpServers": {
    "google-workspace": {
      "command": "/absolute/path/to/Google-Workspace-MCP/.venv/bin/workspace-mcp",
      "args": [],
      "env": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "WORKSPACE_MCP_ENABLE_WRITE_TOOLS": "true",
        "WORKSPACE_MCP_REQUIRE_BIOMETRIC": "true",
        "WORKSPACE_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

---

## Development & Verification

Run the full verification suite using `make`:
```bash
make check      # Runs linter (ruff), type checker (mypy), and test suite (pytest)
make test       # Runs pytest with coverage (must pass >= 85%)
make lint       # Runs ruff code formatting checks
make typecheck  # Runs mypy static type analysis
make schemas    # Regenerates lazy MCP tool schemas
```

---

## License

This project is licensed under the [MIT License](LICENSE).

