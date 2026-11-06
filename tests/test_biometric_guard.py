"""Tests for native OS biometric verification guard."""

from __future__ import annotations

import subprocess
import pytest

from workspace.exceptions import BiometricHardwareUnavailable, BiometricVerificationDenied
from workspace_mcp.biometric_guard import (
    _verify_macos_osascript,
    format_write_prompt,
    verify_macos,
    verify_user_presence,
    verify_windows,
)


def test_format_write_prompt_send_email() -> None:
    prompt = format_write_prompt(
        "send_email",
        {
            "to": ["alice@example.com", "bob@example.com"],
            "subject": "Weekly Sprint Update",
            "attachments": ["/tmp/report.pdf"],
        },
    )
    assert "alice@example.com, bob@example.com" in prompt
    assert "Weekly Sprint Update" in prompt
    assert "+1 attachment" in prompt


def test_format_write_prompt_create_calendar_event() -> None:
    prompt = format_write_prompt(
        "create_calendar_event",
        {
            "summary": "Technical Sync",
            "start": "2026-11-06T15:00:00Z",
            "calendar": "Engineering",
        },
    )
    assert "Technical Sync" in prompt
    assert "2026-11-06T15:00:00Z" in prompt
    assert "Engineering" in prompt


def test_format_write_prompt_delete_calendar_event() -> None:
    prompt = format_write_prompt(
        "delete_calendar_event",
        {
            "uid": "event-999@example.com",
            "calendar": "Team Calendar",
        },
    )
    assert "event-999@example.com" in prompt
    assert "Team Calendar" in prompt


def test_format_write_prompt_new_tools() -> None:
    up_prompt = format_write_prompt("update_calendar_event", {"uid": "ev-123", "summary": "Project Sync"})
    assert "updating" in up_prompt or "update_calendar_event" in up_prompt
    assert "ev-123" in up_prompt
    assert "Project Sync" in up_prompt

    mv_prompt = format_write_prompt("move_calendar_event", {"uid": "ev-123", "to_calendar": "Contract Clients"})
    assert "moving" in mv_prompt
    assert "Contract Clients" in mv_prompt

    batch_prompt = format_write_prompt("batch_calendar_mutations", {"operations": [{"action": "update"}, {"action": "delete"}]})
    assert "batch of 2 calendar mutation" in batch_prompt


def test_format_write_prompt_fallback() -> None:
    prompt = format_write_prompt("unknown_write_tool", {"foo": "bar"})
    assert "unknown_write_tool" in prompt


def test_verify_macos_swift_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCompletedProcess:
        returncode = 0

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/swift")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockCompletedProcess())

    assert verify_macos("Test prompt") is True


def test_verify_macos_swift_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCompletedProcess:
        returncode = 1

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/swift")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockCompletedProcess())

    with pytest.raises(BiometricVerificationDenied, match="User canceled or denied"):
        verify_macos("Test prompt")


def test_verify_macos_swift_fallback_to_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def mock_run(args, **kwargs):
        calls.append(args[0])
        class MockCompletedProcess:
            returncode = 2 if "swift" in args[0] else 0
        return MockCompletedProcess()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/swift")
    monkeypatch.setattr("subprocess.run", mock_run)

    assert verify_macos("Test prompt") is True
    assert len(calls) == 2
    assert "swift" in calls[0]
    assert "osascript" in calls[1]


def test_verify_macos_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="swift", timeout=5)

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/swift")
    monkeypatch.setattr("subprocess.run", mock_run)

    with pytest.raises(BiometricVerificationDenied, match="timed out"):
        verify_macos("Test prompt")


def test_verify_windows_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCompletedProcess:
        returncode = 0
        stdout = "VerificationSucceeded\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockCompletedProcess())
    assert verify_windows("Test prompt") is True


def test_verify_windows_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCompletedProcess:
        returncode = 0
        stdout = "Canceled\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockCompletedProcess())
    with pytest.raises(BiometricVerificationDenied, match="User canceled"):
        verify_windows("Test prompt")


def test_verify_windows_hardware_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCompletedProcess:
        returncode = 0
        stdout = "DeviceNotPresent\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockCompletedProcess())
    with pytest.raises(BiometricHardwareUnavailable, match="unavailable on this system"):
        verify_windows("Test prompt")


def test_verify_user_presence_unsupported_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(BiometricHardwareUnavailable, match="not supported on platform 'linux'"):
        verify_user_presence("Test prompt")


def test_verify_macos_osascript_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("osascript missing")

    monkeypatch.setattr("subprocess.run", mock_run)
    with pytest.raises(BiometricHardwareUnavailable, match="osascript not found"):
        _verify_macos_osascript("Prompt")


def test_biometric_guard_main_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from workspace_mcp.biometric_guard import main

    monkeypatch.setattr("sys.argv", ["biometric_guard", "--test", "-m", "CLI test"])
    monkeypatch.setattr("workspace_mcp.biometric_guard.verify_user_presence", lambda msg, timeout_seconds: True)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

