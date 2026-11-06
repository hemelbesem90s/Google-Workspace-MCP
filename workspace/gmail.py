"""Gmail IMAP and SMTP client implementing non-destructive PEEK reads and secure sending."""

from __future__ import annotations

import datetime
import email
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import email.utils
import imaplib
import logging
import os
from pathlib import Path
import re
import smtplib
from typing import Any, Dict, List, Optional

from workspace.config import WorkspaceConfig
from workspace.exceptions import GmailError
from workspace.models import AttachmentInfo, EmailMessage, SendEmailResult

logger = logging.getLogger(__name__)


def clean_filename(s: str) -> str:
    """Sanitize filename to prevent directory traversal and remove illegal filesystem characters."""
    base = os.path.basename(s.replace("\\", "/"))
    cleaned = re.sub(r'[\r\n\\/*?:"<>|]', "", base).strip()
    return cleaned or "unnamed_attachment"


def decode_mime_header(header_val: Optional[str]) -> str:
    """Decode RFC 2047 MIME encoded headers into clean Python strings."""
    if not header_val:
        return ""
    try:
        decoded_parts = decode_header(header_val)
        result = []
        for text, charset in decoded_parts:
            if isinstance(text, bytes):
                result.append(text.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(str(text))
        return "".join(result)
    except Exception:
        return str(header_val)


class GmailClient:
    """High-level client for searching, fetching, and sending emails via Gmail."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config

    def check_connection(self) -> Dict[str, Any]:
        """Test IMAP and SMTP connectivity and authentication."""
        results: Dict[str, Any] = {"imap": False, "smtp": False, "errors": {}}

        # IMAP check
        try:
            with imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port) as mail:
                mail.login(self.config.email, self.config.app_password)
                mail.select("inbox", readonly=True)
                results["imap"] = True
        except Exception as exc:
            results["errors"]["imap"] = str(exc)

        # SMTP check
        try:
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, timeout=self.config.timeout_seconds) as server:
                server.login(self.config.email, self.config.app_password)
                results["smtp"] = True
        except Exception as exc:
            results["errors"]["smtp"] = str(exc)

        return results

    def _parse_message(
        self,
        msg_id: str,
        raw_email: bytes,
        download_attachments: bool = False,
        download_dir: Optional[str] = None,
    ) -> EmailMessage:
        """Parse raw email bytes into an EmailMessage model without mutating read flags."""
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_header(msg.get("Subject"))
        sender = decode_mime_header(msg.get("From"))
        date_str = decode_mime_header(msg.get("Date"))

        body_parts: List[str] = []
        html_parts: List[str] = []
        attachments: List[AttachmentInfo] = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition") or "")

                # Check if it's an attachment
                is_attachment = "attachment" in content_disposition.lower() or bool(part.get_filename())
                if is_attachment:
                    raw_fn = decode_mime_header(part.get_filename()) or "unnamed_attachment"
                    safe_fn = clean_filename(raw_fn)
                    payload = part.get_payload(decode=True)

                    if isinstance(payload, bytes):
                        att_info = AttachmentInfo(
                            filename=safe_fn,
                            size=len(payload),
                            downloaded_to=None,
                        )
                        if download_attachments:
                            target_dir = Path(download_dir or os.path.join(os.path.expanduser("~"), "Downloads", "Workspace_Attachments"))
                            target_dir.mkdir(parents=True, exist_ok=True)
                            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            dest_path = target_dir / f"{timestamp}_{safe_fn}"
                            with open(dest_path, "wb") as f_att:
                                f_att.write(payload)
                            att_info.downloaded_to = str(dest_path.absolute())

                        attachments.append(att_info)
                elif content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        body_parts.append(payload.decode(charset, errors="ignore"))
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        html_parts.append(payload.decode(charset, errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
                if msg.get_content_type() == "text/html":
                    html_parts.append(text)
                else:
                    body_parts.append(text)

        body = "\n".join(body_parts).strip()
        if not body and html_parts:
            # Fallback to plain text stripped of HTML tags
            raw_html = "\n".join(html_parts)
            body = re.sub(r"<[^>]+>", " ", raw_html)
            body = re.sub(r"\s+", " ", body).strip()

        return EmailMessage(
            id=msg_id,
            subject=subject,
            sender=sender,
            date=date_str,
            body=body,
            attachments=attachments,
        )

    def search_emails(
        self,
        query: Optional[str] = None,
        subject: Optional[str] = None,
        sender: Optional[str] = None,
        unread: bool = False,
        limit: int = 5,
        download_attachments: bool = False,
        download_dir: Optional[str] = None,
    ) -> List[EmailMessage]:
        """Search Gmail inbox using IMAP and fetch messages using BODY.PEEK[].

        Preserves unread status on the server.
        """
        search_criteria: List[str] = []
        if unread:
            search_criteria.append("UNSEEN")
        if subject:
            # Quote cleanly
            clean_sub = subject.replace('"', '\\"')
            search_criteria.append(f'SUBJECT "{clean_sub}"')
        if sender:
            clean_snd = sender.replace('"', '\\"')
            search_criteria.append(f'FROM "{clean_snd}"')
        if query:
            clean_q = query.replace('"', '\\"')
            search_criteria.append(f'TEXT "{clean_q}"')

        if not search_criteria:
            search_criteria.append("ALL")

        search_query = " ".join(search_criteria)

        try:
            mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
            mail.login(self.config.email, self.config.app_password)
            mail.select("inbox", readonly=True)

            status, response = mail.search(None, search_query)
            if status != "OK":
                mail.logout()
                raise GmailError(f"IMAP search failed with status: {status}")

            email_ids = response[0].split()
            if not email_ids:
                mail.close()
                mail.logout()
                return []

            # Reverse to fetch newest first
            email_ids.reverse()

            results: List[EmailMessage] = []
            count = 0

            for e_id in email_ids:
                if count >= limit:
                    break

                try:
                    # Crucial: Fetch using BODY.PEEK[] to preserve unread status
                    status, data = mail.fetch(e_id, "(BODY.PEEK[])")
                    if status != "OK" or not data or not isinstance(data[0], tuple):
                        continue

                    raw_email = data[0][1]
                    str_id = e_id.decode("utf-8") if isinstance(e_id, bytes) else str(e_id)
                    parsed = self._parse_message(
                        msg_id=str_id,
                        raw_email=raw_email,
                        download_attachments=download_attachments,
                        download_dir=download_dir,
                    )
                    results.append(parsed)
                    count += 1
                except Exception as exc:
                    logger.warning("Error parsing email ID %s: %s", e_id, exc)

            try:
                mail.close()
            except Exception:
                pass
            mail.logout()
            return results

        except Exception as exc:
            if isinstance(exc, GmailError):
                raise
            raise GmailError(f"IMAP operation error: {exc}") from exc

    def get_email(
        self,
        email_id: str,
        download_attachments: bool = False,
        download_dir: Optional[str] = None,
    ) -> EmailMessage:
        """Fetch a specific email by ID using BODY.PEEK[] preserving unread status."""
        try:
            mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
            mail.login(self.config.email, self.config.app_password)
            mail.select("inbox", readonly=True)

            status, data = mail.fetch(email_id, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                mail.close()
                mail.logout()
                raise GmailError(f"Email ID '{email_id}' not found or fetch failed.")

            raw_email = data[0][1]
            parsed = self._parse_message(
                msg_id=email_id,
                raw_email=raw_email,
                download_attachments=download_attachments,
                download_dir=download_dir,
            )

            try:
                mail.close()
            except Exception:
                pass
            mail.logout()
            return parsed

        except Exception as exc:
            if isinstance(exc, GmailError):
                raise
            raise GmailError(f"Failed to fetch email {email_id}: {exc}") from exc

    def send_email(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[str]] = None,
    ) -> SendEmailResult:
        """Send an email message via SMTP SSL."""
        if not to:
            raise GmailError("At least one recipient ('to') is required.")
        if not subject:
            raise GmailError("Email subject cannot be empty.")
        if not body:
            raise GmailError("Email body cannot be empty.")

        sender_header = (
            f"{self.config.sender_name} <{self.config.email}>"
            if self.config.sender_name
            else self.config.email
        )

        msg = MIMEMultipart()
        msg["From"] = sender_header
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()

        all_recipients = list(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
            all_recipients.extend(cc)
        if bcc:
            all_recipients.extend(bcc)

        msg.attach(MIMEText(body, "plain", "utf-8"))

        attached_paths: List[str] = []
        if attachments:
            for file_path_str in attachments:
                file_path = Path(file_path_str).expanduser()
                if not file_path.exists():
                    raise GmailError(f"Attachment file not found: {file_path}")

                part = MIMEBase("application", "octet-stream")
                with open(file_path, "rb") as f_att:
                    part.set_payload(f_att.read())
                encoders.encode_base64(part)
                safe_name = clean_filename(file_path.name)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{safe_name}"',
                )
                msg.attach(part)
                attached_paths.append(str(file_path.absolute()))

        try:
            with smtplib.SMTP_SSL(
                self.config.smtp_server,
                self.config.smtp_port,
                timeout=self.config.timeout_seconds,
            ) as server:
                server.login(self.config.email, self.config.app_password)
                server.sendmail(self.config.email, all_recipients, msg.as_string())

            return SendEmailResult(
                success=True,
                action="send_email",
                to=to,
                subject=subject,
                attachments=attached_paths,
            )
        except Exception as exc:
            raise GmailError(f"Failed to send email via SMTP: {exc}") from exc
