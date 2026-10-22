"""IKEA account login (Auth0, authorization code + PKCE), session storage and guest tokens.

How login works
---------------
The IKEA login form is protected by a browser challenge, so a command-line program cannot
fill it in. Instead the user signs in once in a normal browser. Auth0 then keeps a session
for that browser. We create our own PKCE pair and ask Auth0 for a code with
``prompt=none`` and ``response_mode=web_message``. Auth0 answers with a small HTML page
on the accounts domain that contains the code. The user copies that page (or its address)
back into the CLI, and we exchange the code for an access token and a refresh token.

The website's own login page is the only allowed redirect address, and it immediately tries
to redeem any ``?code=`` it sees with its own verifier, which uses up the code. The
``web_message`` page avoids that because no website code runs there.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import GUEST_CLIENT_ID, AuthMarket, Paths, Settings, read_private_json, write_private_json
from .errors import ApiError, AuthRequired, IkeaError, LoginError, NetworkError, SessionExpired, UsageError
from .transport import Transport

REFRESH_MARGIN_SECONDS = 300
PENDING_LOGIN_MAX_AGE = 15 * 60


# --------------------------------------------------------------------------- PKCE and URLs


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""

    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def redirect_uri(settings: Settings) -> str:
    return f"https://www.ikea.com/{settings.country}/{settings.language}/profile/login/"


def authorize_url(settings: Settings, challenge: str, state: str, *, mode: str = "page") -> str:
    """Build the /authorize address.

    ``mode="page"``: silent request that shows the code on an accounts.ikea.com page (needs a
    browser that is already signed in). ``mode="redirect"``: normal interactive login that
    ends on www.ikea.com with the code in the address fragment.
    """

    market = settings.auth_market()
    params = {
        "client_id": market.client_id,
        "redirect_uri": redirect_uri(settings),
        "scope": market.scope,
        "audience": market.audience,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "ui_locales": settings.locale,
    }
    if mode == "page":
        params["response_mode"] = "web_message"
        params["prompt"] = "none"
    elif mode == "redirect":
        params["response_mode"] = "fragment"
    else:
        raise UsageError(f"Unknown login mode {mode!r}.")
    return f"https://{market.domain}/authorize?{urlencode(params)}"


# --------------------------------------------------------------------------- parsing pasted input


@dataclass
class AuthResponse:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


# Auth0 writes either {"type":..,"response":{..}} or {type: .., response: {..}}; the inner object is flat JSON.
_WEB_MESSAGE_RE = re.compile(r"authorizationResponse\s*=\s*\{.*?[\"']?response[\"']?\s*:\s*(\{[^{}]*\})", re.S)
_BARE_CODE_RE = re.compile(r"^[A-Za-z0-9._~\-]{16,512}$")


def parse_auth_response(text: str) -> AuthResponse:
    """Extract code/state/error from a pasted page source, a redirect address or a bare code."""

    raw = html.unescape(text or "").strip()
    if not raw:
        return AuthResponse(error="empty", error_description="Nothing was pasted.")
    match = _WEB_MESSAGE_RE.search(raw)
    if match:
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            resp = payload
            return AuthResponse(
                code=resp.get("code"),
                state=resp.get("state"),
                error=resp.get("error"),
                error_description=resp.get("error_description"),
            )
    for token in raw.split():
        if "code=" in token or "error=" in token:
            parts = urlsplit(token)
            query: dict[str, list[str]] = {}
            for chunk in (parts.query, parts.fragment):
                for key, values in parse_qs(chunk).items():
                    query.setdefault(key, values)
            if not parts.scheme:
                for key, values in parse_qs(token.lstrip("?#")).items():
                    query.setdefault(key, values)
            if "code" in query or "error" in query:
                return AuthResponse(
                    code=(query.get("code") or [None])[0],
                    state=(query.get("state") or [None])[0],
                    error=(query.get("error") or [None])[0],
                    error_description=(query.get("error_description") or [None])[0],
                )
    if _BARE_CODE_RE.match(raw):
        return AuthResponse(code=raw)
    return AuthResponse(error="unrecognized", error_description="No authorization code found in the pasted text.")


def explain_auth_error(resp: AuthResponse) -> LoginError:
    if resp.error == "login_required":
        return LoginError(
            "This browser is not signed in to IKEA.",
            hint="Sign in on ikea.com in the same browser (tick 'keep me signed in'), then open the link again.",
        )
    if resp.error in {"empty", "unrecognized"}:
        return LoginError(
            resp.error_description or "No authorization code found.",
            hint="Paste the whole page source, the full ikea.com address, or just the code value.",
        )
    return LoginError(f"IKEA refused the login: {resp.error_description or resp.error}.")


@contextlib.contextmanager
def _defer_interrupt() -> Iterator[None]:
    """Hold back Ctrl-C for a short critical step and deliver it afterwards."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    received: list[Any] = []
    try:
        previous = signal.signal(signal.SIGINT, lambda signum, frame: received.append(signum))
    except (ValueError, OSError):  # pragma: no cover - no signal support here
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if received:
        raise KeyboardInterrupt


# --------------------------------------------------------------------------- JWT helpers


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (only used for expiry and display)."""

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_duration(value: Any, default: float = 3600.0) -> float:
    """Parse ``expires_in`` values such as 86400, "86400" or "720h"."""

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", value)
        if m:
            factor = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
            return float(m.group(1)) * factor
    return default


# --------------------------------------------------------------------------- session model


@dataclass
class Session:
    access_token: str
    expires_at: float
    country: str
    refresh_token: str | None = None
    id_token: str | None = None
    source: str = "login"
    created_at: float = field(default_factory=time.time)
    refreshed_at: float | None = None

    @classmethod
    def from_token_response(cls, data: dict[str, Any], country: str, source: str) -> Session:
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise LoginError("IKEA did not return an access token.")
        expires_at = jwt_claims(access).get("exp")
        if not isinstance(expires_at, (int, float)):
            expires_at = time.time() + parse_duration(data.get("expires_in"), 86400)
        return cls(
            access_token=access,
            expires_at=float(expires_at),
            country=country,
            refresh_token=data.get("refresh_token") or None,
            id_token=data.get("id_token") or None,
            source=source,
        )

    def expires_in(self) -> float:
        return self.expires_at - time.time()

    def is_expired(self, margin: float = 0) -> bool:
        return self.expires_in() <= margin

    def summary(self) -> dict[str, Any]:
        """Non-secret facts about the session, for `ikea auth status`."""

        access = jwt_claims(self.access_token)
        ident = jwt_claims(self.id_token) if self.id_token else {}
        loyalty = access.get("https://accounts.ikea.com/loyaltyPrograms")
        return {
            "logged_in": True,
            "country": self.country,
            "name": ident.get("name") or ident.get("given_name"),
            "email": ident.get("email"),
            "customer_type": access.get("https://accounts.ikea.com/customerType"),
            "loyalty_programs": loyalty if isinstance(loyalty, list) else ([loyalty] if loyalty else []),
            "expires_at": _iso(self.expires_at),
            "expires_in_seconds": max(0, int(self.expires_in())),
            "can_refresh": bool(self.refresh_token),
            "source": self.source,
        }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises
                return
            except OSError:
                continue
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SessionStore:
    """One session file per market, written atomically with mode 0600."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def _file(self, country: str) -> Path:
        return self.paths.state_dir / f"session-{country}.json"

    def load(self, country: str) -> Session | None:
        """Return the stored session, None if there is none, or raise if the file is damaged."""

        path = self._file(country)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
            if not isinstance(session.access_token, str) or not isinstance(session.expires_at, (int, float)):
                raise TypeError("bad session fields")
            return session
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise SessionExpired(
                "Your saved IKEA session could not be read (the file is damaged).",
                hint="Run `ikea auth login` to sign in again, or `ikea auth logout --local-only` to remove it.",
            ) from exc

    def save(self, session: Session) -> None:
        write_private_json(self._file(session.country), asdict(session))

    def delete(self, country: str) -> bool:
        path = self._file(country)
        if path.exists():
            path.unlink()
            return True
        return False

    @contextlib.contextmanager
    def lock(self, country: str) -> Iterator[None]:
        """Serialize changes to the session: IKEA rotates refresh tokens, so two parallel refreshes would log the user out."""

        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.paths.state_dir / f".session-{country}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_fd(fd)
            yield
        finally:
            _unlock_fd(fd)
            os.close(fd)

    # Pending login (PKCE verifier between `login --start` and `login --finish`)

    @property
    def _pending_file(self) -> Path:
        return self.paths.state_dir / "login-pending.json"

    def save_pending(self, data: dict[str, Any]) -> None:
        write_private_json(self._pending_file, data)

    def load_pending(self) -> dict[str, Any] | None:
        data = read_private_json(self._pending_file)
        if not isinstance(data, dict) or time.time() - _number(data.get("created_at")) > PENDING_LOGIN_MAX_AGE:
            return None
        return data

    def clear_pending(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._pending_file.unlink()


# --------------------------------------------------------------------------- Auth0 calls


def _token_endpoint(market: AuthMarket) -> str:
    return f"https://{market.domain}/oauth/token"


def _post_token(transport: Transport, market: AuthMarket, body: dict[str, Any]) -> dict[str, Any]:
    resp = transport.request(
        "POST", _token_endpoint(market), json_body=body, headers={"content-type": "application/json"}, what="IKEA login"
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not isinstance(data, dict):
        err = data.get("error") if isinstance(data, dict) else None
        desc = data.get("error_description") if isinstance(data, dict) else None
        if err in {"invalid_grant", "invalid_request"}:
            raise LoginError(
                f"IKEA rejected the login code or token: {desc or err}.",
                hint="Codes work once and expire after a few minutes. Start again with `ikea auth login`.",
            )
        raise ApiError(f"IKEA login returned HTTP {resp.status_code}: {desc or err or resp.reason_phrase}", status=resp.status_code)
    return data


def exchange_code(transport: Transport, settings: Settings, code: str, verifier: str) -> dict[str, Any]:
    market = settings.auth_market()
    return _post_token(
        transport,
        market,
        {
            "grant_type": "authorization_code",
            "client_id": market.client_id,
            "code_verifier": verifier,
            "code": code,
            "redirect_uri": redirect_uri(settings),
            "scope": market.scope,
        },
    )


def refresh_tokens(transport: Transport, settings: Settings, refresh_token: str) -> dict[str, Any]:
    market = settings.auth_market()
    return _post_token(
        transport,
        market,
        {
            "grant_type": "refresh_token",
            "client_id": market.client_id,
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri(settings),
            "scope": market.scope,
        },
    )


def revoke_token(transport: Transport, settings: Settings, token: str) -> bool:
    market = settings.auth_market()
    resp = transport.request(
        "POST",
        f"https://{market.domain}/oauth/revoke",
        json_body={"client_id": market.client_id, "token": token},
        headers={"content-type": "application/json"},
        what="IKEA login",
    )
    return resp.status_code in (200, 204)


def normalize_cookie(raw: str) -> str:
    """Accept `value`, `auth0=value` or a whole Cookie header; return a Cookie header value."""

    raw = raw.strip().strip(";")
    if not raw:
        raise UsageError("No cookie was given.")
    if "=" not in raw:
        return f"auth0={raw}"
    pairs = [p.strip() for p in raw.split(";") if "=" in p]
    wanted = [p for p in pairs if p.split("=", 1)[0].strip() in {"auth0", "auth0_compat"}]
    if not wanted:
        raise UsageError("The cookie text does not contain an `auth0` cookie from the IKEA accounts site.")
    return "; ".join(wanted)


def code_from_cookie(transport: Transport, settings: Settings, cookie: str, challenge: str, state: str) -> AuthResponse:
    """Use a browser's Auth0 session cookie to get a code without any browser interaction."""

    url = authorize_url(settings, challenge, state, mode="page")
    resp = transport.request("GET", url, headers={"cookie": normalize_cookie(cookie), "accept": "text/html"}, what="IKEA login")
    if resp.status_code != 200:
        raise LoginError(f"IKEA login returned HTTP {resp.status_code} for the session cookie.")
    return parse_auth_response(resp.text)


# --------------------------------------------------------------------------- guest tokens


def fetch_guest_token(transport: Transport, country: str) -> dict[str, Any]:
    data = transport.json(
        "POST",
        "https://api.ingka.ikea.com/guest/token",
        json_body={"retailUnit": country},
        headers={"x-client-id": GUEST_CLIENT_ID, "accept": "application/json;version=2", "content-type": "application/json"},
        what="IKEA guest login",
    )
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ApiError("IKEA did not return a guest token.")
    return {
        "access_token": data["access_token"],
        "expires_at": time.time() + parse_duration(data.get("expires_in"), 3600),
        "country": country,
    }


# --------------------------------------------------------------------------- token provider


class Auth:
    """Gives API calls a valid token, refreshing or falling back to a guest token as needed."""

    def __init__(self, settings: Settings, paths: Paths, transport: Transport, *, access_token: str | None = None) -> None:
        self.settings = settings
        self.paths = paths
        self.transport = transport
        self.store = SessionStore(paths)
        self._override = access_token or os.environ.get("IKEA_ACCESS_TOKEN") or None

    # -- user sessions

    def session(self) -> Session | None:
        if self._override:
            claims = jwt_claims(self._override)
            return Session(
                access_token=self._override,
                expires_at=float(claims.get("exp") or time.time() + 3600),
                country=self.settings.country,
                source="environment",
            )
        return self.store.load(self.settings.country)

    def can_refresh(self) -> bool:
        if self._override:
            return False
        s = self.store.load(self.settings.country)
        return bool(s and s.refresh_token)

    def user_token(self) -> str:
        s = self.session()
        if s is None:
            raise AuthRequired("This command needs your IKEA account, and you are not logged in.")
        if not s.is_expired(REFRESH_MARGIN_SECONDS):
            return s.access_token
        if not s.refresh_token:
            if not s.is_expired():
                return s.access_token
            raise SessionExpired()
        return self.refresh().access_token

    def refresh(self, *, force: bool = False, rejected_token: str | None = None) -> Session:
        """Renew the access token.

        Runs under the session lock and re-reads the file first, so parallel commands refresh only once.
        ``rejected_token``: the access token IKEA just refused; if the stored one differs, another process
        already renewed it and that one is used instead.
        """

        if self._override:
            raise UsageError("IKEA_ACCESS_TOKEN is set; it cannot be refreshed.")
        country = self.settings.country
        with self.store.lock(country):
            current = self.store.load(country)
            if current is None:
                raise AuthRequired()
            if rejected_token is not None and current.access_token != rejected_token:
                return current  # renewed by another process meanwhile
            if not force and rejected_token is None and not current.is_expired(REFRESH_MARGIN_SECONDS):
                return current  # another process refreshed it while we waited
            if not current.refresh_token:
                raise SessionExpired(hint="This session was imported without a refresh token. Run `ikea auth login`.")
            with _defer_interrupt():
                return self._rotate(current)

    def _rotate(self, current: Session) -> Session:
        # IKEA invalidates the old refresh token as soon as it answers, so the new one is written to disk
        # before anything else can fail. Ctrl-C is held back for this short step by the caller.
        try:
            data = refresh_tokens(self.transport, self.settings, current.refresh_token or "")
        except LoginError as exc:
            raise SessionExpired(
                "Your IKEA session has ended (the refresh token was rejected).", hint="Run `ikea auth login` to sign in again."
            ) from exc
        except NetworkError as exc:
            raise NetworkError(
                "Could not reach IKEA to renew your session.",
                hint="Try again. If it keeps failing with 'session expired', IKEA may have renewed the session "
                "without the answer arriving; then run `ikea auth login`.",
            ) from exc
        new_refresh = data.get("refresh_token") if isinstance(data, dict) else None
        if isinstance(new_refresh, str) and new_refresh and new_refresh != current.refresh_token:
            current.refresh_token = new_refresh
            self.store.save(current)
        fresh = Session.from_token_response(data, current.country, current.source)
        fresh.created_at = current.created_at
        fresh.refreshed_at = time.time()
        if not fresh.refresh_token:
            fresh.refresh_token = current.refresh_token
        if not fresh.id_token:
            fresh.id_token = current.id_token
        self.store.save(fresh)
        return fresh

    def logout(self, *, revoke: bool = True, forget_guest: bool = False) -> dict[str, Any]:
        country = self.settings.country
        revoked = False
        revoke_error = None
        with self.store.lock(country):
            try:
                s = self.store.load(country)
            except SessionExpired:
                s = None  # damaged file: nothing to revoke, just remove it
            if s and s.refresh_token and revoke:
                try:
                    revoked = revoke_token(self.transport, self.settings, s.refresh_token)
                except IkeaError as exc:
                    revoke_error = exc.message
            removed = self.store.delete(country)
        guest_removed = self.forget_guest() if forget_guest else False
        return {
            "removed_local_session": removed,
            "revoked_refresh_token": revoked,
            "revoke_error": revoke_error,
            "removed_guest_session": guest_removed,
        }

    def complete_login(self, data: dict[str, Any], source: str) -> Session:
        session = Session.from_token_response(data, self.settings.country, source)
        claims = jwt_claims(session.access_token)
        market = claims.get("https://accounts.ikea.com/retailUnit")
        if isinstance(market, str) and market.lower() != self.settings.country:
            raise LoginError(f"That login belongs to the IKEA {market.upper()} market, not {self.settings.country.upper()}.")
        if session.is_expired():
            raise LoginError("That access token has already expired.")
        with self.store.lock(self.settings.country):
            self.store.save(session)
        return session

    # -- guest

    def guest_token(self) -> str:
        data = read_private_json(self.paths.guest_file)
        if (
            isinstance(data, dict)
            and data.get("country") == self.settings.country
            and isinstance(data.get("access_token"), str)
            and _number(data.get("expires_at")) - time.time() > REFRESH_MARGIN_SECONDS
        ):
            return data["access_token"]
        fresh = fetch_guest_token(self.transport, self.settings.country)
        write_private_json(self.paths.guest_file, fresh)
        return fresh["access_token"]

    def forget_guest(self) -> bool:
        """Drop the stored guest token. The guest cart and lists can't be reached afterwards."""

        if self.paths.guest_file.exists():
            self.paths.guest_file.unlink()
            return True
        return False

    def shopper_token(self, *, guest: bool = False) -> tuple[str, bool]:
        """Token for cart and lists: the account if logged in, otherwise a guest. Returns (token, is_guest)."""

        if not guest and self.session() is not None:
            return self.user_token(), False
        return self.guest_token(), True
