"""Thin HTTP layer: one place for headers, timeouts and turning failures into IkeaError."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from . import __version__
from .errors import ApiError, IkeaError, NetworkError, NotFound

USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 ikea-cli/{__version__}"
ORIGIN = "https://www.ikea.com"
DEFAULT_TIMEOUT = 30.0


def new_request_id() -> str:
    return str(uuid.uuid4())


class Transport:
    def __init__(self, http: httpx.Client | None = None, *, timeout: float = DEFAULT_TIMEOUT, retries: int = 1) -> None:
        self._owns = http is None
        self.http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self.retries = retries

    def close(self) -> None:
        if self._owns:
            self.http.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        data: dict[str, str] | None = None,
        what: str = "IKEA",
    ) -> httpx.Response:
        merged = {
            "user-agent": USER_AGENT,
            "origin": ORIGIN,
            "referer": ORIGIN + "/",
            "accept": "application/json",
        }
        merged.update(headers or {})
        attempt = 0
        while True:
            try:
                return self.http.request(method, url, headers=merged, params=params, json=json_body, content=content, data=data)
            except httpx.TimeoutException as exc:
                err: Exception = exc
                message = f"{what} did not answer in time."
            except httpx.TransportError as exc:
                err = exc
                message = f"Could not reach {what}: {exc.__class__.__name__}."
            # Only idempotent requests are retried.
            if attempt >= self.retries or method.upper() not in {"GET", "HEAD"}:
                raise NetworkError(message, hint="Check your internet connection and try again.") from err
            attempt += 1
            time.sleep(0.5 * attempt)

    def json(self, method: str, url: str, *, what: str = "IKEA", ok: tuple[int, ...] = (200, 201), **kwargs: Any) -> Any:
        resp = self.request(method, url, what=what, **kwargs)
        if resp.status_code not in ok:
            raise api_error_from_response(resp, what)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"{what} returned a response that is not JSON.", status=resp.status_code) from exc


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def api_error_from_response(resp: httpx.Response, what: str) -> IkeaError:
    """Build a readable error from the different error shapes IKEA services use."""

    status = resp.status_code
    body: Any = None
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = None
    details: Any = None
    if isinstance(body, dict):
        errors = body.get("errors")
        parts: list[str] = []
        if isinstance(errors, list):
            for e in errors:
                if isinstance(e, dict):
                    parts.append(str(e.get("description") or e.get("message") or e.get("code") or ""))
        for key in ("items", "fields"):
            entries = body.get(key)
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict) and e.get("description"):
                        label = e.get("itemNo") or e.get("jsonPath") or ""
                        parts.append(f"{label}: {e['description']}" if label else str(e["description"]))
        if body.get("error_description"):
            parts.append(str(body["error_description"]))
        elif isinstance(body.get("error"), str):
            parts.append(body["error"])
        if not parts and body.get("detail"):
            parts.append(str(body["detail"]))
        if not parts and body.get("title"):
            parts.append(str(body["title"]))
        parts = [p for p in parts if p]
        if parts:
            message = "; ".join(parts)
        codes = [e.get("code") for e in errors if isinstance(e, dict) and e.get("code")] if isinstance(errors, list) else []
        if codes:
            details = {"codes": codes}
    if message is None:
        message = _short(resp.text) if resp.text and "<html" not in resp.text[:200].lower() else resp.reason_phrase
    text = f"{what} returned HTTP {status}: {message}" if message else f"{what} returned HTTP {status}."
    if status == 404:
        return NotFound(text, details=details)
    return ApiError(text, status=status, details=details)
