"""Exception hierarchy for Google Workspace SDK."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base exception for all Workspace errors."""


class WorkspaceAuthError(WorkspaceError):
    """Raised when authentication fails or credentials are missing."""


class GmailError(WorkspaceError):
    """Raised when a Gmail IMAP or SMTP operation fails."""


class CalendarError(WorkspaceError):
    """Raised when a CalDAV operation fails."""


class EventNotFoundError(CalendarError):
    """Raised when a calendar event is not found."""


class BiometricError(WorkspaceError):
    """Base exception for biometric presence verification errors."""


class BiometricVerificationDenied(BiometricError):
    """Raised when user cancels or fails biometric verification."""


class BiometricHardwareUnavailable(BiometricError):
    """Raised when biometric hardware or OS verification mechanism is unavailable."""
