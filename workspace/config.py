"""Configuration loader and credential resolution for Google Workspace."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DEFAULT_KEYRING_SERVICE = "grobler_gmail"
DEFAULT_IMAP_SERVER = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_SERVER = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_CALDAV_BASE_URL = "https://calendar.google.com/calendar/dav/{email}/"


class WorkspaceConfig(BaseModel):
    """Configuration settings and credentials for Google Workspace."""

    model_config = ConfigDict(extra="ignore")

    email: str = Field(..., description="Google Workspace email address")
    app_password: str = Field(..., description="App-specific password for IMAP/SMTP/CalDAV")
    sender_name: Optional[str] = Field(None, description="Display name for outgoing emails")
    imap_server: str = Field(DEFAULT_IMAP_SERVER, description="IMAP server hostname")
    imap_port: int = Field(DEFAULT_IMAP_PORT, description="IMAP SSL port")
    smtp_server: str = Field(DEFAULT_SMTP_SERVER, description="SMTP server hostname")
    smtp_port: int = Field(DEFAULT_SMTP_PORT, description="SMTP SSL port")
    caldav_base_url: str = Field(DEFAULT_CALDAV_BASE_URL, description="Base URL template for CalDAV")
    keyring_service: str = Field(DEFAULT_KEYRING_SERVICE, description="OS Keychain service name")
    timeout_seconds: int = Field(30, description="Network timeout in seconds")

    def __repr__(self) -> str:
        masked_pw = "***" if self.app_password else ""
        return f"WorkspaceConfig(email='{self.email}', app_password='{masked_pw}', imap_server='{self.imap_server}')"

    def get_caldav_url(self) -> str:
        """Format the CalDAV discovery endpoint for the configured email."""
        import urllib.parse
        encoded_email = urllib.parse.quote(self.email)
        return self.caldav_base_url.format(email=encoded_email)


def _load_from_macos_system_keyring(service_name: str, email_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """On macOS, query /usr/bin/python3 to access Keychain within the existing system ACL."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/python3"):
        return None
    try:
        import subprocess
        script = """
import keyring, json, os, sys
svc = sys.argv[1]
hint = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

# Check bundled configs
for k in ("config", "gmail_config", "workspace_config"):
    raw = keyring.get_password(svc, k)
    if raw:
        try:
            p = json.loads(raw)
            if isinstance(p, dict):
                sec = p.get("workspace") or p.get("gmail") or p.get("calendar") or p
                if sec.get("email") and sec.get("app_password"):
                    print(json.dumps(sec))
                    sys.exit(0)
        except Exception:
            pass

em = hint or os.environ.get("WORKSPACE_EMAIL") or os.environ.get("GROBLER_GMAIL_EMAIL") or keyring.get_password(svc, "email")
pw = None
if em:
    pw = keyring.get_password(svc, em)
if not pw:
    pw = keyring.get_password(svc, "app_password")

if em and pw:
    im = keyring.get_password(svc, "imap_server") or "imap.gmail.com"
    sm = keyring.get_password(svc, "smtp_server") or "smtp.gmail.com"
    print(json.dumps({"email": em, "app_password": pw, "imap_server": im, "smtp_server": sm}))
    sys.exit(0)
sys.exit(1)
"""
        proc = subprocess.run(
            ["/usr/bin/python3", "-c", script, service_name, email_hint or ""],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
    except Exception as exc:
        logger.debug("Failed to query macOS system keyring bridge: %s", exc)
    return None


def _load_from_keyring(service_name: str, email_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve credentials from OS Keychain (macOS Keychain / Windows Credential Manager)."""
    # On macOS, try system python first if running in a venv to avoid GUI permission prompts
    if sys.platform == "darwin" and sys.executable != "/usr/bin/python3":
        res = _load_from_macos_system_keyring(service_name, email_hint)
        if res:
            return res

    try:
        import keyring
    except ImportError:
        logger.debug("keyring library not installed; skipping keyring lookup.")
        return None

    try:
        # 1. Try bundled JSON configs in keyring
        for key_name in ("config", "gmail_config", "workspace_config"):
            raw_cfg = keyring.get_password(service_name, key_name)
            if raw_cfg:
                try:
                    parsed = json.loads(raw_cfg)
                    if isinstance(parsed, dict):
                        section = parsed.get("workspace") or parsed.get("gmail") or parsed.get("calendar") or parsed
                        if section.get("email") and section.get("app_password"):
                            return section
                except json.JSONDecodeError:
                    pass

        # 2. Try individual keys
        email_addr = (
            email_hint
            or os.environ.get("WORKSPACE_EMAIL")
            or os.environ.get("GROBLER_GMAIL_EMAIL")
            or os.environ.get("GROBLER_CALENDAR_EMAIL")
            or keyring.get_password(service_name, "email")
        )
        app_pw = None
        if email_addr:
            app_pw = keyring.get_password(service_name, email_addr)
        if not app_pw:
            app_pw = keyring.get_password(service_name, "app_password")

        if email_addr and app_pw:
            imap_srv = keyring.get_password(service_name, "imap_server") or DEFAULT_IMAP_SERVER
            smtp_srv = keyring.get_password(service_name, "smtp_server") or DEFAULT_SMTP_SERVER
            return {
                "email": email_addr,
                "app_password": app_pw,
                "imap_server": imap_srv,
                "smtp_server": smtp_srv,
            }
    except Exception as exc:
        logger.debug("Error retrieving credentials from keyring: %s", exc)

    return None


def get_default_config_search_paths() -> List[str]:
    """Return standard candidate file paths for workspace configuration."""
    candidates: List[Optional[str]] = [
        os.environ.get("WORKSPACE_CONFIG_PATH"),
        os.environ.get("GROBLER_GMAIL_CONFIG"),
        os.environ.get("GROBLER_CALENDAR_CONFIG"),
        str(Path.home() / ".gemini" / "config" / "gmail_config.json"),
        str(Path.home() / ".gemini" / "config" / "workspace_config.json"),
        str(Path.home() / "LAB" / "26031I_GRBL_Email_Synthesizer" / "config.json"),
        "config.json",
    ]
    return [c for c in candidates if c]


def load_workspace_config(
    config_path: Optional[str] = None,
    keyring_service: Optional[str] = None,
    email: Optional[str] = None,
    app_password: Optional[str] = None,
) -> WorkspaceConfig:
    """Load and validate Workspace credentials with robust fallback order:
    1. Explicit function arguments.
    2. Environment variables (WORKSPACE_EMAIL, WORKSPACE_APP_PASSWORD, etc.).
    3. JSON configuration file (custom or standard discovery paths).
    4. OS Keychain (via keyring library).
    """
    service = keyring_service or os.environ.get("WORKSPACE_KEYRING_SERVICE", DEFAULT_KEYRING_SERVICE)

    # 1. Direct environment variable overrides
    env_email = email or os.environ.get("WORKSPACE_EMAIL")
    env_password = app_password or os.environ.get("WORKSPACE_APP_PASSWORD")
    sender_name = os.environ.get("WORKSPACE_SENDER_NAME")
    imap_server = os.environ.get("WORKSPACE_IMAP_SERVER", DEFAULT_IMAP_SERVER)
    imap_port = int(os.environ.get("WORKSPACE_IMAP_PORT", str(DEFAULT_IMAP_PORT)))
    smtp_server = os.environ.get("WORKSPACE_SMTP_SERVER", DEFAULT_SMTP_SERVER)
    smtp_port = int(os.environ.get("WORKSPACE_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    caldav_url = os.environ.get("WORKSPACE_CALDAV_URL", DEFAULT_CALDAV_BASE_URL)

    if env_email and env_password:
        return WorkspaceConfig(
            email=env_email,
            app_password=env_password,
            sender_name=sender_name,
            imap_server=imap_server,
            imap_port=imap_port,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            caldav_base_url=caldav_url,
            keyring_service=service,
            timeout_seconds=30,
        )

    # 2. Check JSON configuration files
    search_paths = [config_path] if config_path else get_default_config_search_paths()
    seen = set()

    for raw_path in search_paths:
        if not raw_path:
            continue
        exp_path = str(Path(raw_path).expanduser())
        if exp_path in seen:
            continue
        seen.add(exp_path)

        if os.path.exists(exp_path):
            try:
                with open(exp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                section = data.get("workspace") or data.get("gmail") or data.get("calendar") or data
                file_email = env_email or section.get("email")
                file_pw = env_password or section.get("app_password")

                if file_email and not file_pw:
                    # Email in file, lookup password in keyring
                    kr_data = _load_from_keyring(service, email_hint=file_email)
                    if kr_data and kr_data.get("app_password"):
                        file_pw = kr_data["app_password"]

                if file_email and file_pw:
                    return WorkspaceConfig(
                        email=file_email,
                        app_password=file_pw,
                        sender_name=sender_name or section.get("sender_name") or section.get("display_name"),
                        imap_server=section.get("imap_server", imap_server),
                        imap_port=int(section.get("imap_port", imap_port)),
                        smtp_server=section.get("smtp_server", smtp_server),
                        smtp_port=int(section.get("smtp_port", smtp_port)),
                        caldav_base_url=section.get("caldav_base_url", caldav_url),
                        keyring_service=service,
                        timeout_seconds=int(section.get("timeout_seconds", 30)),
                    )
            except Exception as exc:
                logger.debug("Failed reading config at %s: %s", exp_path, exc)

    # 3. Fallback to OS Keychain
    kr_data = _load_from_keyring(service, email_hint=env_email)
    if kr_data and kr_data.get("email") and kr_data.get("app_password"):
        return WorkspaceConfig(
            email=kr_data["email"],
            app_password=kr_data["app_password"],
            sender_name=sender_name or kr_data.get("sender_name"),
            imap_server=kr_data.get("imap_server", imap_server),
            imap_port=int(kr_data.get("imap_port", imap_port)),
            smtp_server=kr_data.get("smtp_server", smtp_server),
            smtp_port=int(kr_data.get("smtp_port", smtp_port)),
            caldav_base_url=caldav_url,
            keyring_service=service,
            timeout_seconds=30,
        )

    from workspace.exceptions import WorkspaceAuthError
    raise WorkspaceAuthError(
        f"Google Workspace credentials not found. Set WORKSPACE_EMAIL and WORKSPACE_APP_PASSWORD, "
        f"store them in OS Keychain under service '{service}', or provide a config file."
    )
