"""Checks against the real IKEA website. Skipped unless you opt in:

    IKEA_LIVE=1 uv run pytest -m live

Only guest sessions and public endpoints are used. To also run read-only checks on your own
account (cart view, lists, orders, loyalty; nothing is changed), additionally set
IKEA_LIVE_ACCOUNT=1 and IKEA_ACCESS_TOKEN.
"""

from __future__ import annotations

import os
import time

import pytest

from ikea_cli.client import IkeaClient
from ikea_cli.config import Paths, Settings

LIVE = os.environ.get("IKEA_LIVE") == "1"
ACCOUNT_TOKEN = os.environ.get("IKEA_ACCESS_TOKEN") if os.environ.get("IKEA_LIVE_ACCOUNT") == "1" else None

pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason="set IKEA_LIVE=1 to run live checks")]

LACK = "30449908"  # LACK side table, a long-running item


@pytest.fixture
def guest():
    with IkeaClient(Settings(country="ca", language="en"), paths=Paths.default(), guest=True) as client:
        yield client
        time.sleep(0.5)  # stay polite


def test_search_and_filters(guest):
    result = guest.search("desk", limit=5, sort="price-asc")
    assert result["total"] > 0 and result["items"]
    prices = [i["price"] for i in result["items"]]
    assert prices == sorted(prices)
    colors = next(f for f in result["filters"] if f["parameter"] == "f-colors")
    filtered = guest.search("desk", limit=5, filters={"colors": [colors["values"][0]["id"]]})
    assert 0 < filtered["total"] <= result["total"]


def test_suggest_and_categories(guest):
    assert guest.suggest("kal")
    cats = guest.categories()
    assert len(cats) > 50
    assert guest.search(category=cats[0]["id"], limit=1)["total"] > 0


def test_product(guest):
    p = guest.product(LACK)
    assert p["name"] == "LACK"
    assert p["details_available"] and p["packages"] and p["dimensions"]


def test_stores_and_stock(guest):
    stores = guest.stores(refresh=True)
    assert len(stores) >= 10
    stock = guest.stock([LACK])
    assert stock[0]["stores"]


def test_delivery(guest):
    store = next(s for s in guest.stores() if s.get("kind") == "IKEA STORE")
    result = guest.delivery([(LACK, 1), ("10623220", 1)], postal_code="M5V 3L9", store=store["id"])
    assert result["province"] == "ON"
    assert result["items"][0]["home_delivery"] is not None


def test_guest_cart_cycle(guest):
    guest.cart_clear()
    cart = guest.cart_add([(LACK, 2)])
    assert cart["guest"] and cart["items"][0]["quantity"] == 2
    assert guest.cart_set([(LACK, 3)])["items"][0]["quantity"] == 3
    assert guest.cart_remove([LACK])["empty"]
    guest.cart_add([(LACK, 1)])
    assert guest.cart_clear()["empty"]


def test_guest_list_cycle(guest):
    created = guest.list_create("ikea-cli live test")
    try:
        lst = guest.list_add([(LACK, 1)], list_ref=created["list_id"])
        assert lst["items"][0]["item_no"] == LACK
        assert guest.list_remove([LACK], list_ref=created["list_id"])["items"] == []
    finally:
        guest.list_delete(created["list_id"])


@pytest.mark.skipif(not ACCOUNT_TOKEN, reason="set IKEA_LIVE_ACCOUNT=1 and IKEA_ACCESS_TOKEN for read-only account checks")
def test_account_read_only():
    with IkeaClient(Settings(), paths=Paths.default(), access_token=ACCOUNT_TOKEN) as client:
        assert "items" in client.cart()
        assert isinstance(client.lists(), list)
        orders = client.orders(limit=5)
        assert orders["total"] is not None
        if orders["purchases"]:
            first = orders["purchases"][0]["id"]
            assert client.order(first)["id"] == first
        assert "points" in client.loyalty()
        assert "offers" in client.rewards()
