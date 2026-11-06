"""Google Workspace MCP Server implementing Model Context Protocol over stdio."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

from workspace.calendar import CalendarClient
from workspace.exceptions import (
    BiometricHardwareUnavailable,
    BiometricVerificationDenied,
    CalendarError,
    GmailError,
    WorkspaceAuthError,
)
from workspace.gmail import GmailClient
from workspace_mcp.biometric_guard import format_write_prompt, verify_user_presence
from workspace_mcp.config import MCPConfig
from workspace_mcp.tools import (
    WRITE_TOOL_NAMES,
    ToolRegistry,
    register_all_tools,
)

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = (
    "This server provides access to Google Workspace (Gmail and Google Calendar). "
    "Read-only tools (check_connection_status, search_emails, get_email, list_calendars, "
    "list_calendar_events) connect via IMAP using non-destructive BODY.PEEK[] queries "
    "(preserving unread message flags) and RFC 4791 CalDAV REPORT queries. "
    "Write tools (send_email, create_calendar_event, delete_calendar_event, update_calendar_event, "
    "move_calendar_event, batch_calendar_mutations) allow sending emails, mutating calendar events, "
    "and executing atomic bulk mutations with a unified biometric gate. All write tools require "
    "explicit confirmation (confirm=true) and are strictly gated by native hardware biometric "
    "presence verification (Touch ID on macOS / Windows Hello on Windows)."
)


def _error_result(payload: Dict[str, Any]) -> CallToolResult:
    """Format an error payload into an MCP CallToolResult."""
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, default=str, ensure_ascii=False),
            )
        ],
        isError=True,
    )


class WorkspaceMCPServer:
    """MCP server wrapper managing SDK clients and tool execution."""

    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self.registry = ToolRegistry()
        register_all_tools(self.registry)

        sdk_config = config.load_sdk_config()
        self.gmail_client = GmailClient(sdk_config)
        self.calendar_client = CalendarClient(sdk_config)

    def tool_is_enabled(self, name: str) -> bool:
        """Check if the given tool is enabled according to configuration."""
        if name in WRITE_TOOL_NAMES:
            return self.config.enable_write_tools
        return True

    async def aclose(self) -> None:
        """Clean up resources if needed."""


def create_server(mcp_server: WorkspaceMCPServer) -> Server:
    """Configure and return the low-level MCP server instance."""
    app = Server("google-workspace")

    @app.list_tools()
    async def list_tools() -> List[Tool]:
        tools: List[Tool] = []
        for registered in mcp_server.registry.list_tools():
            if not mcp_server.tool_is_enabled(registered.name):
                continue

            annotations = None
            if registered.name in WRITE_TOOL_NAMES:
                annotations = ToolAnnotations(destructiveHint=True, readOnlyHint=False)

            tools.append(
                Tool(
                    name=registered.name,
                    description=registered.description,
                    inputSchema=registered.get_schema(),
                    annotations=annotations,
                )
            )
        return tools

    @app.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any] | None) -> CallToolResult:
        if not mcp_server.tool_is_enabled(name):
            return _error_result(
                {
                    "error": "ToolDisabled",
                    "message": f"Tool '{name}' is disabled in configuration.",
                    "tool": name,
                }
            )

        tool = mcp_server.registry.get(name)
        if not tool:
            return _error_result(
                {
                    "error": "ToolNotFound",
                    "message": f"Tool '{name}' not found.",
                    "tool": name,
                }
            )

        args = arguments or {}

        # Gated write operations
        if name in WRITE_TOOL_NAMES:
            if not args.get("confirm"):
                return _error_result(
                    {
                        "error": "ConfirmationRequired",
                        "message": (
                            f"Action '{name}' requires explicit confirmation. "
                            "Pass confirm=true after user authorization."
                        ),
                        "tool": name,
                    }
                )

            if mcp_server.config.require_biometric:
                prompt_msg = format_write_prompt(name, args)
                try:
                    await asyncio.to_thread(
                        verify_user_presence,
                        prompt_msg,
                        mcp_server.config.biometric_timeout_seconds,
                    )
                except BiometricVerificationDenied as exc:
                    return _error_result(
                        {
                            "error": "BiometricDenied",
                            "message": str(exc),
                            "tool": name,
                        }
                    )
                except BiometricHardwareUnavailable as exc:
                    return _error_result(
                        {
                            "error": "BiometricUnavailable",
                            "message": str(exc),
                            "tool": name,
                        }
                    )
                except Exception as exc:
                    return _error_result(
                        {
                            "error": "BiometricError",
                            "message": f"Biometric verification error: {exc}",
                            "tool": name,
                        }
                    )

        try:
            result = await tool.execute(mcp_server.gmail_client, mcp_server.calendar_client, args)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(result, default=str, ensure_ascii=False),
                    )
                ]
            )
        except (ValueError, ValidationError) as exc:
            return _error_result(
                {
                    "error": "ValidationError",
                    "message": str(exc),
                    "tool": name,
                }
            )
        except (GmailError, CalendarError, WorkspaceAuthError) as exc:
            return _error_result(
                {
                    "error": exc.__class__.__name__,
                    "message": str(exc),
                    "tool": name,
                }
            )
        except Exception as exc:
            logger.exception("Unexpected error executing tool %s", name)
            return _error_result(
                {
                    "error": "InternalError",
                    "message": str(exc),
                    "tool": name,
                }
            )

    return app


def generate_tool_schemas(target_dir: str | Path) -> None:
    """Generate lazy MCP tool schemas and instructions.md into target directory."""
    out = Path(target_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    registry = ToolRegistry()
    register_all_tools(registry)

    for tool in registry.list_tools():
        schema_data = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.get_schema(),
        }
        dest = out / f"{tool.name}.json"
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(schema_data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    instructions_file = out / "instructions.md"
    instructions_content = (
        "This server connects to Google Workspace (Gmail and Google Calendar). "
        "Read-only tools (check_connection_status, search_emails, get_email, list_calendars, list_calendar_events) "
        "are enabled by default and use IMAP BODY.PEEK[] queries so messages remain unread on the server. "
        "All write tools (send_email, create_calendar_event, delete_calendar_event) strictly require explicit "
        "user confirmation (confirm=true) and are gated by native OS hardware biometric verification (Touch ID / Windows Hello). "
        "Never disclose credentials. Always verify parameters before performing write operations.\n"
    )
    with open(instructions_file, "w", encoding="utf-8") as f:
        f.write(instructions_content)

    print(f"Generated {len(registry.list_tools())} tool schemas in {out}")


async def run_server(config: MCPConfig) -> None:
    """Run the MCP server over standard I/O."""
    mcp_server = WorkspaceMCPServer(config)
    app = create_server(mcp_server)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        await mcp_server.aclose()


def main() -> None:
    """CLI entrypoint for workspace-mcp."""
    if getattr(sys.stdout, "encoding", None) != "utf-8":
        reconfig = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfig):
            reconfig(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Google Workspace Hybrid MCP Server (IMAP PEEK + CalDAV + Biometrics)"
    )
    parser.add_argument(
        "--generate-schemas",
        metavar="DIR",
        type=str,
        help="Generate lazy MCP tool JSON schemas in the specified directory and exit",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Check IMAP, SMTP, and CalDAV credentials and connection health",
    )
    parser.add_argument(
        "--test-biometric",
        action="store_true",
        help="Trigger a native OS biometric verification prompt for testing",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="workspace-mcp 0.1.0",
        help="Show program's version number and exit",
    )

    args = parser.parse_args()

    if args.generate_schemas:
        generate_tool_schemas(args.generate_schemas)
        sys.exit(0)

    if args.test_biometric:
        prompt = "Google Workspace MCP: Biometric Verification Test"
        try:
            verify_user_presence(prompt)
            print("Biometric verification succeeded.")
            sys.exit(0)
        except BiometricVerificationDenied as exc:
            print(f"Biometric verification denied: {exc}", file=sys.stderr)
            sys.exit(1)
        except BiometricHardwareUnavailable as exc:
            print(f"Biometric hardware unavailable: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"Biometric verification error: {exc}", file=sys.stderr)
            sys.exit(3)

    config = MCPConfig.from_env()

    if args.test_connection:
        try:
            sdk_config = config.load_sdk_config()
            print(f"Workspace configuration resolved for: {sdk_config.email}")
            gmail = GmailClient(sdk_config)
            cal = CalendarClient(sdk_config)
            email_status = gmail.check_connection()
            print(f"IMAP SSL ({sdk_config.imap_server}): {'OK' if email_status['imap'] else 'FAILED'}")
            print(f"SMTP SSL ({sdk_config.smtp_server}): {'OK' if email_status['smtp'] else 'FAILED'}")
            calendars = cal.discover_calendars()
            print(f"CalDAV ({sdk_config.caldav_base_url}): OK ({len(calendars)} calendars found)")
            for c in calendars:
                print(f"  - {c.name}: {c.href}")
            sys.exit(0)
        except Exception as exc:
            print(f"Connection test error: {exc}", file=sys.stderr)
            sys.exit(1)

    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO), stream=sys.stderr)
    try:
        asyncio.run(run_server(config))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
