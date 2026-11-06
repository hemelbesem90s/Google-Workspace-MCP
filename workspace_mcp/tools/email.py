"""Email tools for Google Workspace MCP (IMAP PEEK search/fetch and SMTP send)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from pydantic import Field

from workspace_mcp.tools.base import BaseTool, ToolSchema

if TYPE_CHECKING:
    from workspace.calendar import CalendarClient
    from workspace.gmail import GmailClient


class SearchEmailsSchema(ToolSchema):
    """Input parameters for search_emails."""

    query: Optional[str] = Field(
        None,
        description="Search term across subject, body, or headers (IMAP TEXT search)",
    )
    subject: Optional[str] = Field(
        None,
        description="Filter specifically by email subject line",
    )
    sender: Optional[str] = Field(
        None,
        description="Filter specifically by sender email address or display name",
    )
    unread: bool = Field(
        False,
        description="Whether to return unread (UNSEEN) emails only",
    )
    limit: int = Field(
        5,
        description="Maximum number of emails to retrieve (default: 5, max: 50)",
        ge=1,
        le=50,
    )
    download_attachments: bool = Field(
        False,
        description="Whether to automatically download attachment files to disk",
    )
    download_dir: Optional[str] = Field(
        None,
        description="Target directory for downloaded attachments (defaults to system Downloads/Workspace_Attachments)",
    )


class SearchEmailsTool(BaseTool):
    """Tool to search and retrieve emails using IMAP BODY.PEEK[]."""

    name = "search_emails"
    description = (
        "Search and retrieve email messages from Gmail via IMAP. Crucially uses BODY.PEEK[] "
        "so reading messages never alters their unread status on the server."
    )
    input_schema = SearchEmailsSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = SearchEmailsSchema.model_validate(arguments)
        messages = await asyncio.to_thread(
            gmail_client.search_emails,
            query=args.query,
            subject=args.subject,
            sender=args.sender,
            unread=args.unread,
            limit=args.limit,
            download_attachments=args.download_attachments,
            download_dir=args.download_dir,
        )
        return {
            "count": len(messages),
            "emails": [m.model_dump() for m in messages],
        }


class GetEmailSchema(ToolSchema):
    """Input parameters for get_email."""

    email_id: str = Field(
        ...,
        description="IMAP message sequence ID or UID of the email to retrieve",
    )
    download_attachments: bool = Field(
        False,
        description="Whether to download attachment files to disk",
    )
    download_dir: Optional[str] = Field(
        None,
        description="Target directory for downloaded attachments",
    )


class GetEmailTool(BaseTool):
    """Tool to fetch a specific email message by ID using IMAP BODY.PEEK[]."""

    name = "get_email"
    description = (
        "Retrieve a single email message by its IMAP sequence ID or UID. "
        "Uses BODY.PEEK[] so the email remains marked unread if it was unread."
    )
    input_schema = GetEmailSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = GetEmailSchema.model_validate(arguments)
        message = await asyncio.to_thread(
            gmail_client.get_email,
            email_id=args.email_id,
            download_attachments=args.download_attachments,
            download_dir=args.download_dir,
        )
        return message.model_dump()


class SendEmailSchema(ToolSchema):
    """Input parameters for send_email."""

    to: List[str] = Field(
        ...,
        description="List of primary recipient email addresses",
        min_length=1,
    )
    subject: str = Field(
        ...,
        description="Email subject line",
        min_length=1,
    )
    body: str = Field(
        ...,
        description="Plain text or markdown content of the email message",
        min_length=1,
    )
    cc: Optional[List[str]] = Field(
        None,
        description="Optional list of carbon-copy (CC) recipient email addresses",
    )
    bcc: Optional[List[str]] = Field(
        None,
        description="Optional list of blind carbon-copy (BCC) recipient email addresses",
    )
    attachments: Optional[List[str]] = Field(
        None,
        description="Optional list of absolute file paths to attach",
    )
    confirm: bool = Field(
        False,
        description="Explicit confirmation to send the email. Must be set to true.",
    )


class SendEmailTool(BaseTool):
    """Tool to send an email via SMTP SSL strictly gated by hardware biometrics."""

    name = "send_email"
    description = (
        "Send an email message via SMTP SSL. Requires explicit confirmation (confirm=true) "
        "and is strictly gated by native hardware biometric verification (Touch ID / Windows Hello)."
    )
    input_schema = SendEmailSchema

    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        args = SendEmailSchema.model_validate(arguments)
        if not args.confirm:
            raise ValueError("Explicit confirmation (confirm=true) is required to send an email.")

        result = await asyncio.to_thread(
            gmail_client.send_email,
            to=args.to,
            subject=args.subject,
            body=args.body,
            cc=args.cc,
            bcc=args.bcc,
            attachments=args.attachments,
        )
        return result.model_dump()
