"""Checks against the real IKEA website. Skipped unless you opt in:

    IKEA_LIVE=1 uv run pytest -m live
    IKEA_LIVE=1 IKEA_LIVE_MARKETS=us/en,de/de,jp/ja uv run pytest -m live

Only guest sessions and public endpoints are used. Each market picks its own sample product
(from a search), store (the first full-size store in IKEA's store list) and postal code (that
store's), unless IKEA_LIVE_STORE / IKEA_LIVE_POSTAL_CODE are set. To also run read-only checks
on your own account (cart view, lists, orders, loyalty; nothing is changed), additionally set
IKEA_LIVE_ACCOUNT=1 and IKEA_ACCESS_TOKEN; that runs in the first market listed.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import httpx
import pytest

from ikea_cli import auth
from ikea_cli.client import IkeaClient
from ikea_cli.config import Paths, Settings

LIVE = os.environ.get("IKEA_LIVE") == "1"
ACCOUNT_TOKEN = os.environ.get("IKEA_ACCESS_TOKEN") if os.environ.get("IKEA_LIVE_ACCOUNT") == "1" else None
MARKETS = [m.strip() for m in os.environ.get("IKEA_LIVE_MARKETS", "ca/en").split(",") if m.strip()]

pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason="set IKEA_LIVE=1 to run live checks")]


def _settings(market: str) -> Settings:
    country, _, language = market.partition("/")
    return Settings(country=country, language=language or None)


@pytest.fixture(params=MARKETS)
def guest(request):
    with IkeaClient(_settings(request.param), paths=Paths.default(), guest=True) as client:
        yield client
        time.sleep(0.5)  # stay polite


def sample_item(client: IkeaClient) -> str:
    """An ordinary product sold online in this market."""

    for text in ("LACK", "KALLAX", "BILLY"):
        for item in client.search(text, limit=10)["items"]:
            if item["item_type"] == "ART" and item["online_sellable"] is not False and item["price"]:
                return str(item["item_no"])
    pytest.skip("no sample product found")


def sample_store(client: IkeaClient) -> dict[str, Any]:
    wanted = os.environ.get("IKEA_LIVE_STORE")
    stores = client.stores()
    if wanted:
        return client.find_store(wanted)
    return next((s for s in stores if "STORE" in str(s.get("kind") or "").upper() and s.get("postal_code")), stores[0])


def test_search_and_filters(guest):
    result = guest.search("LACK", limit=5, sort="price-asc")
    assert result["total"] > 0 and result["items"]
    prices = [i["price"] for i in result["items"] if i["price"] is not None]
    assert prices == sorted(prices)
    colors = next((f for f in result["filters"] if f["parameter"] == "f-colors" and f["values"]), None)
    if colors:
        filtered = guest.search("LACK", limit=5, filters={"colors": [colors["values"][0]["id"]]})
        assert 0 < filtered["total"] <= result["total"]


def test_suggest_and_categories(guest):
    assert guest.suggest("kal")
    cats = guest.categories()
    assert cats
    assert guest.search(category=cats[0]["id"], limit=1)["total"] > 0


def test_product(guest):
    item = sample_item(guest)
    p = guest.product(item)
    assert p["item_no"] == item and p["name"] and p["currency"] == guest.market.currency
    assert p["details_available"]


def test_stores_and_stock(guest):
    stores = guest.stores(refresh=True)
    assert stores
    stock = guest.stock([sample_item(guest)])
    assert stock[0].get("error") is None


def needs(client: IkeaClient, feature: str) -> None:
    if not client.market.offers(feature):
        pytest.skip(f"IKEA does not serve {feature} for this market through the shared services")


def test_delivery(guest):
    needs(guest, "delivery")
    store = sample_store(guest)
    postal = os.environ.get("IKEA_LIVE_POSTAL_CODE") or store.get("postal_code")
    if not postal:
        pytest.skip("no postal code in the store list; set IKEA_LIVE_POSTAL_CODE")
    result = guest.delivery([(sample_item(guest), 1)], postal_code=postal, store=store["id"])
    assert result["items"] and result["items"][0]["home_delivery"] is not None


def test_guest_cart_cycle(guest):
    needs(guest, "cart")
    item = sample_item(guest)
    guest.cart_clear()
    cart = guest.cart_add([(item, 2)])
    assert cart["guest"] and cart["items"][0]["quantity"] == 2
    assert guest.cart_set([(item, 3)])["items"][0]["quantity"] == 3
    assert guest.cart_remove([item])["empty"]
    guest.cart_add([(item, 1)])
    assert guest.cart_clear()["empty"]


def test_guest_list_cycle(guest):
    needs(guest, "lists")
    item = sample_item(guest)
    created = guest.list_create("ikea-cli live test")
    try:
        lst = guest.list_add([(item, 1)], list_ref=created["list_id"])
        assert lst["items"][0]["item_no"] == item
        assert guest.list_remove([item], list_ref=created["list_id"])["items"] == []
    finally:
        guest.list_delete(created["list_id"])


def test_login_link(guest):
    """IKEA's login service accepts our login link for this market (no sign-in happens)."""

    if not guest.market.auth_domain:
        pytest.skip("this market signs in without a public client id")
    _, challenge = auth.make_pkce()
    url = auth.authorize_url(guest.settings, challenge, secrets.token_urlsafe(8), mode="redirect")
    resp = httpx.get(url, follow_redirects=True, timeout=30, headers={"user-agent": "Mozilla/5.0"})
    assert resp.status_code == 200
    assert "error" not in resp.url.path and "error=" not in str(resp.url), "IKEA's login service rejected the link"


@pytest.mark.skipif(not ACCOUNT_TOKEN, reason="set IKEA_LIVE_ACCOUNT=1 and IKEA_ACCESS_TOKEN for read-only account checks")
def test_account_read_only():
    with IkeaClient(_settings(MARKETS[0]), paths=Paths.default(), access_token=ACCOUNT_TOKEN) as client:
        assert "items" in client.cart()
        assert isinstance(client.lists(), list)
        orders = client.orders(limit=5)
        assert orders["total"] is not None
        if orders["purchases"]:
            first = orders["purchases"][0]["id"]
            assert client.order(first)["id"] == first
        assert "points" in client.loyalty()
        assert "offers" in client.rewards()
