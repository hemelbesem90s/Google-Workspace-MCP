"""Tests for workspace config loading and credential resolution."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from workspace.config import (
    WorkspaceConfig,
    load_workspace_config,
)
from workspace.exceptions import WorkspaceAuthError


def test_workspace_config_repr_masks_password() -> None:
    cfg = WorkspaceConfig(
        email="operator@example.com",
        app_password="super-secret-password",
    )
    repr_str = repr(cfg)
    assert "operator@example.com" in repr_str
    assert "super-secret-password" not in repr_str
    assert "***" in repr_str


def test_load_workspace_config_from_explicit_args() -> None:
    cfg = load_workspace_config(
        email="arg_user@example.com",
        app_password="arg_password_123",
    )
    assert cfg.email == "arg_user@example.com"
    assert cfg.app_password == "arg_password_123"


def test_load_workspace_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_EMAIL", "env_user@example.com")
    monkeypatch.setenv("WORKSPACE_APP_PASSWORD", "env_password_456")
    monkeypatch.setenv("WORKSPACE_SENDER_NAME", "Env Sender")
    monkeypatch.setenv("WORKSPACE_IMAP_SERVER", "custom.imap.com")
    monkeypatch.setenv("WORKSPACE_SMTP_SERVER", "custom.smtp.com")

    cfg = load_workspace_config()
    assert cfg.email == "env_user@example.com"
    assert cfg.app_password == "env_password_456"
    assert cfg.sender_name == "Env Sender"
    assert cfg.imap_server == "custom.imap.com"
    assert cfg.smtp_server == "custom.smtp.com"


def test_load_workspace_config_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_EMAIL", raising=False)
    monkeypatch.delenv("WORKSPACE_APP_PASSWORD", raising=False)

    cfg_file = tmp_path / "test_config.json"
    cfg_file.write_text(
        json.dumps({
            "workspace": {
                "email": "file_user@example.com",
                "app_password": "file_password_789",
                "imap_server": "file.imap.com",
            }
        }),
        encoding="utf-8",
    )

    cfg = load_workspace_config(config_path=str(cfg_file))
    assert cfg.email == "file_user@example.com"
    assert cfg.app_password == "file_password_789"
    assert cfg.imap_server == "file.imap.com"


def test_workspace_config_get_caldav_url() -> None:
    cfg = WorkspaceConfig(
        email="user@example.com",
        app_password="secret-password",
        caldav_base_url="https://cal.example.com/dav/{email}/",
    )
    assert cfg.get_caldav_url() == "https://cal.example.com/dav/user%40example.com/"


def test_get_default_config_search_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from workspace.config import get_default_config_search_paths
    monkeypatch.setenv("WORKSPACE_CONFIG_PATH", "/custom/path.json")
    paths = get_default_config_search_paths()
    assert "/custom/path.json" in paths


def test_load_from_macos_system_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    from workspace.config import _load_from_macos_system_keyring

    class MockProcess:
        returncode = 0
        stdout = json.dumps({"email": "sys@example.com", "app_password": "sys_password"})

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("os.path.exists", lambda path: True)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockProcess())

    res = _load_from_macos_system_keyring("test_service")
    assert res is not None
    assert res["email"] == "sys@example.com"
    assert res["app_password"] == "sys_password"


def test_load_from_keyring_bundled_and_individual(monkeypatch: pytest.MonkeyPatch) -> None:
    from workspace.config import _load_from_keyring

    mock_keyring = {}

    class FakeKeyring:
        @staticmethod
        def get_password(service, key):
            return mock_keyring.get((service, key))

    monkeypatch.setattr("sys.modules", {**{"keyring": FakeKeyring}, **pytest.importorskip("sys").modules})
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("sys.executable", "/usr/bin/python3")

    # 1. Bundled JSON config
    mock_keyring[("svc1", "config")] = json.dumps({"email": "bundled@example.com", "app_password": "pw1"})
    res1 = _load_from_keyring("svc1")
    assert res1 is not None
    assert res1["email"] == "bundled@example.com"

    # 2. Individual keys
    mock_keyring[("svc2", "email")] = "indiv@example.com"
    mock_keyring[("svc2", "app_password")] = "pw2"
    res2 = _load_from_keyring("svc2")
    assert res2 is not None
    assert res2["email"] == "indiv@example.com"


def test_load_workspace_config_missing_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_EMAIL", raising=False)
    monkeypatch.delenv("WORKSPACE_APP_PASSWORD", raising=False)
    monkeypatch.delenv("GROBLER_GMAIL_EMAIL", raising=False)
    monkeypatch.delenv("GROBLER_CALENDAR_EMAIL", raising=False)

    monkeypatch.setattr("workspace.config._load_from_keyring", lambda service, email_hint=None: None)
    monkeypatch.setattr("workspace.config._load_from_macos_system_keyring", lambda service, email_hint=None: None)

    with pytest.raises(WorkspaceAuthError, match="Google Workspace credentials not found"):
        load_workspace_config(config_path="/nonexistent/path/config.json")


