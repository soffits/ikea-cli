from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import Router, access_token, add_guest, load_fixture, make_jwt

from ikea_cli import auth
from ikea_cli.config import Paths, Settings
from ikea_cli.errors import AuthRequired, ConfigError, LoginError, NetworkError, SessionExpired, UsageError
from ikea_cli.transport import Transport

TOKEN_URL = "https://ca.accounts.ikea.com/oauth/token"
REVOKE_URL = "https://ca.accounts.ikea.com/oauth/revoke"


def make_auth(router: Router, **kwargs) -> auth.Auth:
    transport = Transport(httpx.Client(transport=httpx.MockTransport(router)))
    return auth.Auth(Settings(), Paths.default(), transport, **kwargs)


# --------------------------------------------------------------------------- PKCE and URLs


def test_pkce_challenge_matches_verifier():
    verifier, challenge = auth.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


@pytest.mark.parametrize(("mode", "response_mode", "prompt"), [("page", "web_message", "none"), ("redirect", "fragment", None)])
def test_authorize_url(mode, response_mode, prompt):
    url = auth.authorize_url(Settings(), "CHALLENGE", "STATE", mode=mode)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.netloc == "ca.accounts.ikea.com"
    assert q["redirect_uri"] == "https://www.ikea.com/ca/en/profile/login/"
    assert q["code_challenge"] == "CHALLENGE"
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == "STATE"
    assert "offline_access" in q["scope"]
    assert q["audience"] == "https://retail.api.ikea.com"
    assert q["response_mode"] == response_mode
    assert q.get("prompt") == prompt


def test_authorize_url_rejects_unknown_mode():
    with pytest.raises(UsageError):
        auth.authorize_url(Settings(), "c", "s", mode="popup")


def test_unconfigured_market_needs_auth_settings():
    with pytest.raises(ConfigError):
        Settings(country="se", language="sv").auth_market()
    market = Settings(country="se", language="sv", auth_domain="se.accounts.example", auth_client_id="abc").auth_market()
    assert market.domain == "se.accounts.example"


# --------------------------------------------------------------------------- pasted input


def test_parse_web_message_page_with_unquoted_keys():
    resp = auth.parse_auth_response(load_fixture("authorize_page.html"))
    assert resp.code == "TESTCODE_abcdefghijklmnop"
    assert resp.state == "STATE123"
    assert resp.error is None


def test_parse_web_message_login_required():
    resp = auth.parse_auth_response(load_fixture("authorize_login_required.html"))
    assert resp.code is None
    assert resp.error == "login_required"
    err = auth.explain_auth_error(resp)
    assert "not signed in" in err.message


def test_parse_html_escaped_page_source():
    escaped = load_fixture("authorize_page.html").replace('"', "&quot;")
    assert auth.parse_auth_response(escaped).code == "TESTCODE_abcdefghijklmnop"


@pytest.mark.parametrize(
    "text",
    [
        "https://www.ikea.com/ca/en/profile/login/?code=ABCDEFGHIJKLMNOPQRST&state=S1",
        "https://www.ikea.com/ca/en/profile/login/#code=ABCDEFGHIJKLMNOPQRST&state=S1",
        "  copied: https://www.ikea.com/ca/en/profile/login/?state=S1&code=ABCDEFGHIJKLMNOPQRST \n",
        "?code=ABCDEFGHIJKLMNOPQRST&state=S1",
    ],
)
def test_parse_redirect_addresses(text):
    resp = auth.parse_auth_response(text)
    assert resp.code == "ABCDEFGHIJKLMNOPQRST"
    assert resp.state == "S1"


def test_parse_bare_code_and_garbage():
    assert auth.parse_auth_response("ABCDEFGHIJKLMNOPQRST").code == "ABCDEFGHIJKLMNOPQRST"
    assert auth.parse_auth_response("hello world").error == "unrecognized"
    assert auth.parse_auth_response("   ").error == "empty"
    err = auth.parse_auth_response("https://www.ikea.com/ca/en/profile/login/?error=access_denied&error_description=Nope")
    assert err.error == "access_denied"
    assert "Nope" in auth.explain_auth_error(err).message


def test_normalize_cookie():
    assert auth.normalize_cookie("s%3Aabc") == "auth0=s%3Aabc"
    assert auth.normalize_cookie("auth0=x; other=1; auth0_compat=y") == "auth0=x; auth0_compat=y"
    with pytest.raises(UsageError):
        auth.normalize_cookie("session=1; other=2")
    with pytest.raises(UsageError):
        auth.normalize_cookie("   ")


# --------------------------------------------------------------------------- helpers


def test_jwt_claims_and_durations():
    token = make_jwt({"exp": 123, "sub": "x"})
    assert auth.jwt_claims(token)["exp"] == 123
    assert auth.jwt_claims("not-a-jwt") == {}
    assert auth.parse_duration("720h") == 720 * 3600
    assert auth.parse_duration(86400) == 86400
    assert auth.parse_duration("90") == 90
    assert auth.parse_duration("soon", 5) == 5


def test_session_from_token_response_uses_jwt_expiry():
    tok = access_token(expires_in=100)
    s = auth.Session.from_token_response({"access_token": tok, "refresh_token": "r1", "expires_in": 999999}, "ca", "login")
    assert 90 < s.expires_in() <= 100
    assert s.refresh_token == "r1"
    summary = s.summary()
    assert summary["can_refresh"] is True
    assert summary["loyalty_programs"] == ["FAMILYCA"]
    assert "access_token" not in json.dumps(summary)
    assert tok not in json.dumps(summary)


def test_session_from_token_response_requires_token():
    with pytest.raises(LoginError):
        auth.Session.from_token_response({}, "ca", "login")


# --------------------------------------------------------------------------- storage


def test_session_store_is_private_and_atomic(isolated_env):
    store = auth.SessionStore(Paths.default())
    s = auth.Session(access_token=access_token(), expires_at=time.time() + 60, country="ca", refresh_token="r")
    store.save(s)
    path = isolated_env / "state" / "session-ca.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".tmp-")]
    loaded = store.load("ca")
    assert loaded is not None and loaded.refresh_token == "r"
    assert store.delete("ca") is True
    assert store.load("ca") is None


@pytest.mark.parametrize("content", ["{broken", "[]", '{"access_token": 5, "expires_at": "x", "country": "ca"}', '{"country": "ca"}'])
def test_damaged_session_file_is_reported(isolated_env, content):
    path = isolated_env / "state" / "session-ca.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(SessionExpired) as exc:
        auth.SessionStore(Paths.default()).load("ca")
    assert "damaged" in exc.value.message
    assert "logout --local-only" in exc.value.hint


# --------------------------------------------------------------------------- token provider


def test_user_token_requires_login(router):
    with pytest.raises(AuthRequired):
        make_auth(router).user_token()


def test_valid_token_is_used_without_network(router):
    a = make_auth(router)
    tok = access_token(expires_in=3600)
    a.store.save(auth.Session(access_token=tok, expires_at=time.time() + 3600, country="ca", refresh_token="r"))
    assert a.user_token() == tok
    assert router.requests == []


def test_refresh_rotates_and_saves(router):
    new_tok = access_token(expires_in=86400)
    router.add("POST", TOKEN_URL, {"access_token": new_tok, "refresh_token": "r2", "expires_in": 86400})
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=10), expires_at=time.time() + 10, country="ca", refresh_token="r1"))
    assert a.user_token() == new_tok
    body = router.body("POST", "/oauth/token")
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "r1"
    assert body["client_id"] == "jUflsyuJkwbC0RQg1i8bgo1dyld6NM5d"
    saved = a.store.load("ca")
    assert saved.refresh_token == "r2"
    assert saved.refreshed_at is not None


def test_refresh_keeps_old_refresh_token_when_not_rotated(router):
    router.add("POST", TOKEN_URL, {"access_token": access_token(), "expires_in": 86400})
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="r1"))
    a.user_token()
    assert a.store.load("ca").refresh_token == "r1"


def test_rejected_refresh_means_session_expired(router):
    router.add("POST", TOKEN_URL, {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."}, status=403)
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="bad"))
    with pytest.raises(SessionExpired) as exc:
        a.user_token()
    assert "ikea auth login" in exc.value.hint


def test_expired_token_without_refresh(router):
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca"))
    with pytest.raises(SessionExpired):
        a.user_token()


def test_environment_token_override(router, monkeypatch):
    tok = access_token()
    monkeypatch.setenv("IKEA_ACCESS_TOKEN", tok)
    a = make_auth(router)
    assert a.user_token() == tok
    assert a.session().source == "environment"
    with pytest.raises(UsageError):
        a.refresh(force=True)


def test_complete_login_rejects_other_market(router):
    a = make_auth(router)
    with pytest.raises(LoginError):
        a.complete_login({"access_token": access_token(**{"https://accounts.ikea.com/retailUnit": "US"})}, "login")


def test_exchange_code_sends_pkce_verifier(router):
    router.add("POST", TOKEN_URL, {"access_token": access_token(), "refresh_token": "r", "id_token": make_jwt({"email": "a@example.com"})})
    a = make_auth(router)
    data = auth.exchange_code(a.transport, a.settings, "CODE", "VERIFIER")
    body = router.body("POST", "/oauth/token")
    assert body == {
        "grant_type": "authorization_code",
        "client_id": "jUflsyuJkwbC0RQg1i8bgo1dyld6NM5d",
        "code_verifier": "VERIFIER",
        "code": "CODE",
        "redirect_uri": "https://www.ikea.com/ca/en/profile/login/",
        "scope": "openid profile email offline_access",
    }
    session = a.complete_login(data, "login")
    assert session.summary()["email"] == "a@example.com"


def test_exchange_code_invalid_grant(router):
    router.add("POST", TOKEN_URL, {"error": "invalid_grant", "error_description": "Invalid authorization code"}, status=403)
    a = make_auth(router)
    with pytest.raises(LoginError) as exc:
        auth.exchange_code(a.transport, a.settings, "CODE", "VERIFIER")
    assert "Invalid authorization code" in exc.value.message


def test_code_from_cookie_sends_cookie_and_parses_page(router):
    router.add("GET", "https://ca.accounts.ikea.com/authorize", load_fixture("authorize_page.html"))
    a = make_auth(router)
    resp = auth.code_from_cookie(a.transport, a.settings, "auth0=abc", "CH", "STATE123")
    assert resp.code == "TESTCODE_abcdefghijklmnop"
    sent = router.last("GET", "/authorize")
    assert sent.headers["cookie"] == "auth0=abc"
    assert sent.url.params["prompt"] == "none"


def test_logout_revokes_and_deletes(router, isolated_env):
    router.add("POST", REVOKE_URL, "", status=200)
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(), expires_at=time.time() + 60, country="ca", refresh_token="r9"))
    result = a.logout()
    assert result == {
        "removed_local_session": True,
        "revoked_refresh_token": True,
        "revoke_error": None,
        "removed_guest_session": False,
    }
    assert router.body("POST", "/oauth/revoke") == {"client_id": "jUflsyuJkwbC0RQg1i8bgo1dyld6NM5d", "token": "r9"}
    assert a.store.load("ca") is None


def test_logout_local_only_does_not_call_ikea(router):
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(), expires_at=time.time() + 60, country="ca", refresh_token="r9"))
    a.logout(revoke=False)
    assert router.requests == []


# --------------------------------------------------------------------------- guest


def test_guest_token_is_cached(router, isolated_env):
    add_guest(router)
    a = make_auth(router)
    t1 = a.guest_token()
    t2 = a.guest_token()
    assert t1 == t2
    assert len([r for r in router.requests if r.url.path == "/guest/token"]) == 1
    sent = router.last("POST", "/guest/token")
    assert sent.headers["x-client-id"] == "b477b9e4-b836-40fb-842f-43db89dd3c52"
    assert json.loads(sent.content) == {"retailUnit": "ca"}
    path = isolated_env / "state" / "guest.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_shopper_token_prefers_account(router):
    add_guest(router)
    a = make_auth(router)
    assert a.shopper_token()[1] is True
    tok = access_token()
    a.store.save(auth.Session(access_token=tok, expires_at=time.time() + 3600, country="ca"))
    assert a.shopper_token() == (tok, False)
    assert a.shopper_token(guest=True)[1] is True


# --------------------------------------------------------------------------- review round: refresh edge cases


def test_refresh_keeps_id_token_when_not_returned(router):
    router.add("POST", TOKEN_URL, {"access_token": access_token(), "refresh_token": "r2"})
    a = make_auth(router)
    id_token = make_jwt({"email": "kept@example.com", "name": "Kept"})
    a.store.save(
        auth.Session(
            access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="r1", id_token=id_token
        )
    )
    a.user_token()
    saved = a.store.load("ca")
    assert saved.id_token == id_token
    assert saved.summary()["email"] == "kept@example.com"


def test_rotated_refresh_token_is_saved_even_if_the_rest_fails(router):
    router.add("POST", TOKEN_URL, {"refresh_token": "r2"})  # no access token: parsing fails after rotation
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="r1"))
    with pytest.raises(LoginError):
        a.user_token()
    assert a.store.load("ca").refresh_token == "r2"


def test_network_error_during_refresh_is_explained(router):
    def down(request):
        raise httpx.ReadTimeout("slow", request=request)

    router.add("POST", TOKEN_URL, down)
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="r1"))
    with pytest.raises(NetworkError) as exc:
        a.user_token()
    assert "renew your session" in exc.value.message
    assert "ikea auth login" in exc.value.hint
    assert exc.value.exit_code == 6
    assert a.store.load("ca").refresh_token == "r1"


@pytest.mark.skipif(not hasattr(signal, "SIGINT") or os.name == "nt", reason="POSIX signals")
def test_ctrl_c_during_refresh_waits_until_the_new_token_is_saved(router):
    def answer_then_interrupt(request):
        os.kill(os.getpid(), signal.SIGINT)
        return httpx.Response(200, json={"access_token": access_token(), "refresh_token": "r2"})

    router.add("POST", TOKEN_URL, answer_then_interrupt)
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(expires_in=-5), expires_at=time.time() - 5, country="ca", refresh_token="r1"))
    with pytest.raises(KeyboardInterrupt):
        a.user_token()
    assert a.store.load("ca").refresh_token == "r2"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_refresh_after_rejection_uses_token_renewed_elsewhere(router):
    a = make_auth(router)
    newer = access_token()
    a.store.save(auth.Session(access_token=newer, expires_at=time.time() + 3600, country="ca", refresh_token="r5"))
    assert a.refresh(rejected_token="older-token").access_token == newer
    assert router.requests == []


def test_guest_token_from_another_market_is_not_reused(router, isolated_env):
    add_guest(router)
    from ikea_cli.config import write_private_json

    write_private_json(
        isolated_env / "state" / "guest.json", {"access_token": "us-guest", "expires_at": time.time() + 9999, "country": "us"}
    )
    assert make_auth(router).guest_token() != "us-guest"
    assert len(router.requests) == 1


def test_damaged_guest_file_gets_a_new_token(router, isolated_env):
    add_guest(router)
    path = isolated_env / "state" / "guest.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"access_token": "x", "expires_at": "soon", "country": "ca"}')
    assert make_auth(router).guest_token() != "x"


def test_pending_login_expires(isolated_env):
    store = auth.SessionStore(Paths.default())
    store.save_pending({"verifier": "v", "state": "s", "country": "ca", "created_at": time.time() - auth.PENDING_LOGIN_MAX_AGE - 5})
    assert store.load_pending() is None
    store.save_pending({"verifier": "v", "state": "s", "country": "ca", "created_at": time.time()})
    assert store.load_pending()["state"] == "s"
    store.save_pending({"verifier": "v", "state": "s", "country": "ca", "created_at": "garbage"})
    assert store.load_pending() is None


# --------------------------------------------------------------------------- review round: login/logout


def test_expired_import_keeps_existing_session(router):
    a = make_auth(router)
    good = auth.Session(access_token=access_token(), expires_at=time.time() + 3600, country="ca", refresh_token="keep")
    a.store.save(good)
    with pytest.raises(LoginError):
        a.complete_login({"access_token": access_token(expires_in=-10)}, "access-token")
    assert a.store.load("ca").refresh_token == "keep"


def test_logout_keeps_guest_session_unless_asked(router, isolated_env):
    add_guest(router)
    a = make_auth(router)
    a.guest_token()
    guest_file = isolated_env / "state" / "guest.json"
    assert a.logout(revoke=False)["removed_guest_session"] is False
    assert guest_file.exists()
    assert a.logout(revoke=False, forget_guest=True)["removed_guest_session"] is True
    assert not guest_file.exists()


def test_logout_removes_damaged_session_file(router, isolated_env):
    path = isolated_env / "state" / "session-ca.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    result = make_auth(router).logout()
    assert result["removed_local_session"] is True
    assert router.requests == []


@pytest.mark.parametrize("action", ["logout", "login"])
def test_logout_and_login_wait_for_the_session_lock(router, action):
    a = make_auth(router)
    a.store.save(auth.Session(access_token=access_token(), expires_at=time.time() + 3600, country="ca", refresh_token="old"))
    done = threading.Event()

    def run():
        if action == "logout":
            a.logout(revoke=False)
        else:
            a.complete_login({"access_token": access_token(), "refresh_token": "new"}, "login")
        done.set()

    with a.store.lock("ca"):
        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.3)
        assert not done.is_set(), f"{action} did not wait for the session lock"
        assert a.store.load("ca").refresh_token == "old"
    worker.join(5)
    assert done.is_set()
