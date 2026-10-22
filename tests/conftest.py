"""Shared test helpers. Tests never touch the network or the developer's own session."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ikea_cli.client import IkeaClient
from ikea_cli.config import Paths, Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def make_jwt(claims: dict[str, Any]) -> str:
    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.signature"


def access_token(expires_in: float = 3600, **extra: Any) -> str:
    claims = {
        "exp": int(time.time() + expires_in),
        "https://accounts.ikea.com/retailUnit": "CA",
        "https://accounts.ikea.com/customerType": "B2C",
        "https://accounts.ikea.com/loyaltyPrograms": ["FAMILYCA"],
        **extra,
    }
    return make_jwt(claims)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every run at a temporary home and drop IKEA_* variables from the real environment."""

    import os

    for key in list(os.environ):
        if key.startswith("IKEA_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IKEA_CLI_HOME", str(tmp_path / "home"))
    # Most tests use one market; tests about choosing a market remove this.
    monkeypatch.setenv("IKEA_COUNTRY", "ca")
    return tmp_path / "home"


Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """Route requests by (method, host, path) to handlers and record everything that was sent."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, str, Handler]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, handler: Handler | Any, status: int = 200) -> Router:
        parsed = httpx.URL(url)
        if not callable(handler):
            payload = handler

            def handler(request: httpx.Request, payload: Any = payload, status: int = status) -> httpx.Response:
                if isinstance(payload, str):
                    return httpx.Response(status, text=payload)
                return httpx.Response(status, json=payload)

        self.routes.append((method.upper(), parsed.host, parsed.path, handler))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, host, path, handler in reversed(self.routes):
            if request.method == method and request.url.host == host and request.url.path == path:
                return handler(request)
        return httpx.Response(599, text=f"no route for {request.method} {request.url}")

    def last(self, method: str | None = None, path_part: str = "") -> httpx.Request:
        for req in reversed(self.requests):
            if (method is None or req.method == method) and path_part in req.url.path:
                return req
        raise AssertionError(f"no request {method} {path_part}")

    def body(self, method: str | None = None, path_part: str = "") -> Any:
        return json.loads(self.last(method, path_part).content)


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def make_client(router: Router, isolated_env: Path) -> Callable[..., IkeaClient]:
    def make(**kwargs: Any) -> IkeaClient:
        settings = kwargs.pop("settings", None) or Settings(**{"country": "ca", **kwargs.pop("settings_kwargs", {})})
        return IkeaClient(settings, paths=Paths.default(), http=httpx.Client(transport=httpx.MockTransport(router)), **kwargs)

    return make


GUEST_URL = "https://api.ingka.ikea.com/guest/token"


def add_guest(router: Router) -> None:
    router.add("POST", GUEST_URL, {"access_token": make_jwt({"guest": True}), "expires_in": "720h", "token_type": "Bearer"})
