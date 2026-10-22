"""Several processes refreshing the same session at once must refresh exactly once.

IKEA rotates refresh tokens: after a refresh the old token is rejected. Without the session lock and the
re-check under it, parallel commands would either fail or refresh several times.
"""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import httpx
from conftest import access_token

from ikea_cli import auth
from ikea_cli.config import Paths, Settings
from ikea_cli.errors import IkeaError
from ikea_cli.transport import Transport

WORKERS = 4


def _paths(home: str) -> Paths:
    root = Path(home)
    return Paths(root / "config", root / "state", root / "cache")


def _worker(home: str, state, lock, barrier) -> str:
    def token_server(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["refresh_token"]
        with lock:
            if sent != state["valid"]:
                state["rejected"] += 1
                return httpx.Response(403, json={"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
            state["count"] += 1
            new = f"refresh-{state['count'] + 1}"
            state["valid"] = new
        time.sleep(0.3)  # keep the race window open after rotating
        return httpx.Response(200, json={"access_token": access_token(expires_in=86400), "refresh_token": new, "expires_in": 86400})

    transport = Transport(httpx.Client(transport=httpx.MockTransport(token_server)))
    a = auth.Auth(Settings(country="ca"), _paths(home), transport)
    barrier.wait(timeout=60)
    try:
        a.user_token()
    except IkeaError as exc:
        return f"error: {exc.message}"
    return "ok"


def test_parallel_refresh_happens_once(tmp_path):
    home = str(tmp_path / "home")
    store = auth.SessionStore(_paths(home))
    store.save(
        auth.Session(access_token=access_token(expires_in=-60), expires_at=time.time() - 60, country="ca", refresh_token="refresh-1")
    )
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        state = manager.dict(valid="refresh-1", count=0, rejected=0)
        lock = manager.Lock()
        barrier = manager.Barrier(WORKERS)
        with ctx.Pool(WORKERS) as pool:
            results = pool.starmap(_worker, [(home, state, lock, barrier)] * WORKERS, chunksize=1)
        assert results == ["ok"] * WORKERS
        assert state["count"] == 1, "the session was refreshed more than once"
        assert state["rejected"] == 0
        assert store.load("ca").refresh_token == state["valid"]
