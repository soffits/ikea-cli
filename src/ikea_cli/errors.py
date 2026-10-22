"""Error types. Each one maps to a stable exit code and a short machine-readable code."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_API = 5
EXIT_NETWORK = 6
EXIT_CONFIG = 7


class IkeaError(Exception):
    """Base class for every error raised by ikea-cli."""

    code = "error"
    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, details: Any = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details is not None:
            out["details"] = self.details
        return out


class UsageError(IkeaError):
    code = "usage"
    exit_code = EXIT_USAGE


class ConfigError(IkeaError):
    code = "config"
    exit_code = EXIT_CONFIG


class AuthRequired(IkeaError):
    code = "auth_required"
    exit_code = EXIT_AUTH

    def __init__(self, message: str = "You are not logged in.", **kwargs: Any) -> None:
        kwargs.setdefault("hint", "Run `ikea auth login` (or `ikea auth import`) first.")
        super().__init__(message, **kwargs)


class SessionExpired(AuthRequired):
    code = "session_expired"

    def __init__(self, message: str = "Your IKEA session has expired.", **kwargs: Any) -> None:
        kwargs.setdefault("hint", "Run `ikea auth login` to sign in again.")
        super().__init__(message, **kwargs)


class LoginError(IkeaError):
    code = "login_failed"
    exit_code = EXIT_AUTH


class NotFound(IkeaError):
    code = "not_found"
    exit_code = EXIT_NOT_FOUND


class ApiError(IkeaError):
    """IKEA answered, but with an error."""

    code = "api_error"
    exit_code = EXIT_API

    def __init__(self, message: str, *, status: int | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.status is not None:
            out["status"] = self.status
        return out


class NetworkError(IkeaError):
    code = "network"
    exit_code = EXIT_NETWORK
