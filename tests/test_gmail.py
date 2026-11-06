"""Tests for Gmail IMAP PEEK email reading and SMTP sending."""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from workspace.config import WorkspaceConfig
from workspace.exceptions import GmailError
from workspace.gmail import GmailClient, clean_filename, decode_mime_header


def test_clean_filename() -> None:
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename(r"C:\Windows\System32\cmd.exe") == "cmd.exe"
    assert clean_filename('invoice:"test"*.pdf') == "invoicetest.pdf"
    assert clean_filename("") == "unnamed_attachment"


def test_decode_mime_header() -> None:
    assert decode_mime_header("Simple ASCII") == "Simple ASCII"
    assert decode_mime_header("=?utf-8?b?VGVzdCBTdWJqZWN0?=") == "Test Subject"
    assert decode_mime_header(None) == ""


def test_check_connection_success(mock_config: WorkspaceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = GmailClient(mock_config)

    mock_imap = MagicMock()
    mock_imap.__enter__.return_value = mock_imap
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_imap)

    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda host, port, timeout: mock_smtp)

    res = client.check_connection()
    assert res["imap"] is True
    assert res["smtp"] is True
    assert res["errors"] == {}


def test_check_connection_failure(mock_config: WorkspaceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = GmailClient(mock_config)

    def mock_imap_err(*args, **kwargs):
        raise ConnectionRefusedError("IMAP down")

    def mock_smtp_err(*args, **kwargs):
        raise ConnectionRefusedError("SMTP down")

    monkeypatch.setattr("imaplib.IMAP4_SSL", mock_imap_err)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_err)

    res = client.check_connection()
    assert res["imap"] is False
    assert res["smtp"] is False
    assert "IMAP down" in res["errors"]["imap"]
    assert "SMTP down" in res["errors"]["smtp"]


def test_search_emails_uses_body_peek(
    mock_config: WorkspaceConfig,
    sample_mime_email: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GmailClient(mock_config)

    mock_mail = MagicMock()
    mock_mail.search.return_value = ("OK", [b"101 102"])
    mock_mail.fetch.return_value = ("OK", [(b"102 (BODY.PEEK[] {123})", sample_mime_email)])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    emails = client.search_emails(query="Project", limit=1)
    assert len(emails) == 1
    email_msg = emails[0]
    assert email_msg.subject == "Project Update & Review"
    assert "Hello team" in email_msg.body
    assert "sender@example.com" in email_msg.sender

    # Crucial security verification: BODY.PEEK[] was used to prevent marking as read
    fetch_calls = mock_mail.fetch.call_args_list
    assert len(fetch_calls) > 0
    for call in fetch_calls:
        args, _ = call
        assert args[1] == "(BODY.PEEK[])"


def test_search_emails_criteria_construction(
    mock_config: WorkspaceConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GmailClient(mock_config)

    mock_mail = MagicMock()
    mock_mail.search.return_value = ("OK", [b""])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    # 1. Unread and subject and sender and query
    client.search_emails(query="urgent", subject="Invoice", sender="acme", unread=True)
    query_str = mock_mail.search.call_args[0][1]
    assert "UNSEEN" in query_str
    assert 'SUBJECT "Invoice"' in query_str
    assert 'FROM "acme"' in query_str
    assert 'TEXT "urgent"' in query_str

    # 2. Defaults to ALL
    client.search_emails()
    assert mock_mail.search.call_args[0][1] == "ALL"


def test_search_emails_with_attachments_download(
    mock_config: WorkspaceConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GmailClient(mock_config)

    # Create email with attachment
    msg = MIMEMultipart()
    msg["Subject"] = "Attachment Test"
    msg["From"] = "sender@example.com"
    msg["To"] = "test@example.com"
    msg["Date"] = "Wed, 06 Nov 2026 12:00:00 +0100"

    body = MIMEText("See attached report.", "plain")
    msg.attach(body)

    att = MIMEText("Sample CSV content line 1,line 2", "plain")
    att.add_header("Content-Disposition", 'attachment; filename="data.csv"')
    msg.attach(att)

    mock_mail = MagicMock()
    mock_mail.search.return_value = ("OK", [b"201"])
    mock_mail.fetch.return_value = ("OK", [(b"201 (BODY.PEEK[])", msg.as_bytes())])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    dl_dir = tmp_path / "downloads"
    emails = client.search_emails(download_attachments=True, download_dir=str(dl_dir))
    assert len(emails) == 1
    assert len(emails[0].attachments) == 1
    att_info = emails[0].attachments[0]
    assert att_info.filename == "data.csv"
    assert att_info.downloaded_to is not None
    assert Path(att_info.downloaded_to).exists()
    assert Path(att_info.downloaded_to).read_text(encoding="utf-8") == "Sample CSV content line 1,line 2"


def test_search_emails_html_only(mock_config: WorkspaceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = GmailClient(mock_config)

    html_email = (
        b"From: sender@example.com\r\n"
        b"Subject: HTML Notice\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><h1>Important</h1><p>Action required immediately.</p></body></html>"
    )

    mock_mail = MagicMock()
    mock_mail.search.return_value = ("OK", [b"888"])
    mock_mail.fetch.return_value = ("OK", [(b"888 (BODY.PEEK[])", html_email)])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    emails = client.search_emails()
    assert len(emails) == 1
    assert "Important" in emails[0].body
    assert "Action required immediately." in emails[0].body


def test_search_emails_imap_failure(mock_config: WorkspaceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = GmailClient(mock_config)

    mock_mail = MagicMock()
    mock_mail.search.return_value = ("NO", [b""])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    with pytest.raises(GmailError, match="IMAP search failed"):
        client.search_emails()


def test_get_email_by_id(
    mock_config: WorkspaceConfig,
    sample_mime_email: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GmailClient(mock_config)

    mock_mail = MagicMock()
    mock_mail.fetch.return_value = ("OK", [(b"555 (BODY.PEEK[])", sample_mime_email)])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    msg = client.get_email("555")
    assert msg.id == "555"
    assert msg.subject == "Project Update & Review"
    mock_mail.fetch.assert_called_with("555", "(BODY.PEEK[])")


def test_get_email_not_found(mock_config: WorkspaceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = GmailClient(mock_config)

    mock_mail = MagicMock()
    mock_mail.fetch.return_value = ("NO", [])
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)

    with pytest.raises(GmailError, match="not found or fetch failed"):
        client.get_email("999999")


def test_send_email_success(
    mock_config: WorkspaceConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GmailClient(mock_config)

    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda host, port, timeout: mock_smtp)

    test_file = tmp_path / "spec.txt"
    test_file.write_text("Specification document text", encoding="utf-8")

    res = client.send_email(
        to=["recipient@example.com"],
        subject="Technical Spec",
        body="Attached is the specification.",
        cc=["lead@example.com"],
        bcc=["archive@example.com"],
        attachments=[str(test_file)],
    )

    assert res.success is True
    assert res.to == ["recipient@example.com"]
    assert res.subject == "Technical Spec"
    assert len(res.attachments) == 1

    mock_smtp.login.assert_called_once_with(mock_config.email, mock_config.app_password)
    mock_smtp.sendmail.assert_called_once()
    send_args = mock_smtp.sendmail.call_args[0]
    assert send_args[0] == mock_config.email
    assert "recipient@example.com" in send_args[1]
    assert "lead@example.com" in send_args[1]
    assert "archive@example.com" in send_args[1]


def test_send_email_validation_errors(mock_config: WorkspaceConfig) -> None:
    client = GmailClient(mock_config)

    with pytest.raises(GmailError, match="recipient"):
        client.send_email(to=[], subject="Sub", body="Body")

    with pytest.raises(GmailError, match="subject"):
        client.send_email(to=["to@example.com"], subject="", body="Body")

    with pytest.raises(GmailError, match="body"):
        client.send_email(to=["to@example.com"], subject="Sub", body="")

    with pytest.raises(GmailError, match="Attachment file not found"):
        client.send_email(
            to=["to@example.com"],
            subject="Sub",
            body="Body",
            attachments=["/nonexistent/attachment.pdf"],
        )
