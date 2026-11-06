"""Native OS biometric presence verification guard for Google Workspace write tools.

Provides fail-closed user presence verification via native OS modals:
- Apple LocalAuthentication (Touch ID / Face ID / Passcode) on macOS with osascript fallback.
- Windows Hello (PIN, Facial Recognition, Fingerprint) via UserConsentVerifier on Windows.
- Fail-closed on Linux or unsupported operating systems.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict

from workspace.exceptions import BiometricHardwareUnavailable, BiometricVerificationDenied

logger = logging.getLogger(__name__)

MACOS_SWIFT_TOUCHID = """
import Foundation
import LocalAuthentication

let context = LAContext()
var error: NSError?
let reason = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Google Workspace Verification"

let policy: LAPolicy = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error)
    ? .deviceOwnerAuthenticationWithBiometrics
    : .deviceOwnerAuthentication

if context.canEvaluatePolicy(policy, error: &error) {
    let semaphore = DispatchSemaphore(value: 0)
    var success = false
    context.evaluatePolicy(policy, localizedReason: reason) { (succ, _) in
        success = succ
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .distantFuture)
    exit(success ? 0 : 1)
} else {
    exit(2)
}
"""

WINDOWS_USER_CONSENT_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]

$prompt = $env:MCP_BIOMETRIC_PROMPT
if (-not $prompt) {
    $prompt = "Google Workspace MCP Verification"
}

try {
    $op = [Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($prompt)
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod([Windows.Security.Credentials.UI.UserConsentVerificationResult])
    $task = $asTask.Invoke($null, @($op))
    $task.Wait()
    $res = $task.Result.ToString()
    if ($res -eq 'Verified') {
        Write-Output 'VerificationSucceeded'
    } else {
        Write-Output $res
    }
} catch {
    Write-Output "Error: $_"
}
"""


def format_write_prompt(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Format an informative, human-readable prompt for write operations.

    Args:
        tool_name: Name of the mutation tool being invoked.
        arguments: Tool input parameters.

    Returns:
        A concise plain-language summary for the OS biometric prompt.
    """
    args = dict(arguments or {})

    if tool_name == "send_email":
        recipients = args.get("to", [])
        if isinstance(recipients, list):
            recipients_str = ", ".join(recipients)
        else:
            recipients_str = str(recipients)
        subject = args.get("subject", "[No Subject]")
        att = args.get("attachments", [])
        att_str = f" (+{len(att)} attachment{'s' if len(att) > 1 else ''})" if att else ""
        return f"Google Workspace MCP: Authorize sending email to {recipients_str}{att_str}\nSubject: '{subject}'"

    if tool_name == "create_calendar_event":
        summary = args.get("summary", "[New Event]")
        start = args.get("start", "")
        cal_name = args.get("calendar", "Primary")
        return f"Google Workspace MCP: Authorize creating event '{summary}' on {start} in calendar '{cal_name}'"

    if tool_name == "delete_calendar_event":
        uid = args.get("uid", "[Event UID]")
        cal_name = args.get("calendar")
        cal_str = f" in calendar '{cal_name}'" if cal_name else ""
        return f"Google Workspace MCP: Authorize deleting calendar event UID '{uid}'{cal_str}"

    return f"Google Workspace MCP: Authorize write tool '{tool_name}'"


def _verify_macos_osascript(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Fallback dialog modal on macOS using AppleScript."""
    escaped = prompt_message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display dialog "{escaped}" '
        'with title "Google Workspace Security Gate" '
        'buttons {"Cancel", "Approve"} default button "Approve" '
        "with icon caution"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode == 0:
            return True
        raise BiometricVerificationDenied("User canceled or denied verification dialog.")
    except subprocess.TimeoutExpired as exc:
        raise BiometricVerificationDenied(f"Verification dialog timed out after {timeout_seconds}s.") from exc
    except FileNotFoundError as exc:
        raise BiometricHardwareUnavailable(f"osascript not found on system: {exc}") from exc


def verify_macos(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Verify presence on macOS via Swift LocalAuthentication or osascript dialog fallback."""
    swift_cmd = shutil.which("swift") or ("/usr/bin/swift" if os.path.exists("/usr/bin/swift") else None)
    if swift_cmd:
        try:
            proc = subprocess.run(
                [swift_cmd, "-e", MACOS_SWIFT_TOUCHID, prompt_message],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
            if proc.returncode == 0:
                return True
            if proc.returncode == 1:
                raise BiometricVerificationDenied("User canceled or denied Touch ID verification.")
            if proc.returncode == 2:
                # Hardware policy cannot be evaluated (e.g. clamshell mode without external Touch ID)
                return _verify_macos_osascript(prompt_message, timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise BiometricVerificationDenied(f"Biometric verification timed out after {timeout_seconds}s.") from exc
        except (BiometricVerificationDenied, BiometricHardwareUnavailable):
            raise
        except OSError:
            pass

    return _verify_macos_osascript(prompt_message, timeout_seconds)


def verify_windows(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Verify presence on Windows via Windows Hello UserConsentVerifier."""
    env = os.environ.copy()
    env["MCP_BIOMETRIC_PROMPT"] = prompt_message

    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Sta",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                WINDOWS_USER_CONSENT_PS,
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BiometricVerificationDenied(f"Biometric verification timed out after {timeout_seconds}s.") from exc
    except FileNotFoundError as exc:
        raise BiometricHardwareUnavailable(f"powershell.exe not found on system: {exc}") from exc

    if proc.returncode != 0:
        err = proc.stderr.strip() or f"Process exited with code {proc.returncode}"
        raise BiometricHardwareUnavailable(f"Windows UserConsentVerifier invocation failed: {err}")

    output = proc.stdout.strip()
    if output in ("VerificationSucceeded", "Verified"):
        return True
    if output in ("Canceled", "RetriesExhausted"):
        raise BiometricVerificationDenied("User canceled or failed biometric verification.")
    if output in ("DeviceNotPresent", "NotConfiguredForUser", "DisabledByPolicy"):
        raise BiometricHardwareUnavailable(f"Biometric verification unavailable on this system: {output}.")

    raise BiometricVerificationDenied(f"Biometric verification failed: {output}")


def verify_user_presence(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Perform native fail-closed biometric presence verification on the active OS.

    Args:
        prompt_message: Explanatory text shown in the OS modal.
        timeout_seconds: Timeout in seconds.

    Returns:
        True if verification succeeded.

    Raises:
        BiometricVerificationDenied: If user denies, cancels, or times out.
        BiometricHardwareUnavailable: If biometric verification is not supported.
    """
    if sys.platform == "darwin":
        return verify_macos(prompt_message, timeout_seconds=timeout_seconds)
    if sys.platform == "win32":
        return verify_windows(prompt_message, timeout_seconds=timeout_seconds)

    raise BiometricHardwareUnavailable(
        f"Biometric verification is not supported on platform '{sys.platform}'."
    )


def main() -> None:
    """CLI diagnostic tool to test biometric verification on the local machine."""
    parser = argparse.ArgumentParser(description="Google Workspace MCP Native Biometric Presence Guard")
    parser.add_argument("--test", action="store_true", help="Trigger a test prompt to verify OS biometric setup")
    parser.add_argument("--message", "-m", default="Google Workspace MCP: Biometric Verification Test", help="Prompt text")
    parser.add_argument("--timeout", type=int, default=60, help="Verification timeout in seconds (default: 60)")
    args = parser.parse_args()

    print(f"Testing native biometric verification on {sys.platform}...")
    print(f"Prompt: {args.message}")
    try:
        verify_user_presence(args.message, timeout_seconds=args.timeout)
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


if __name__ == "__main__":
    main()
