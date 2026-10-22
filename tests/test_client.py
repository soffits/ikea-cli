from __future__ import annotations

import json
import time

import httpx
import pytest
from conftest import access_token, add_guest, load_fixture

from ikea_cli import auth
from ikea_cli.config import Settings
from ikea_cli.errors import ApiError, AuthRequired, NetworkError, NotFound, SessionExpired, UsageError

SEARCH = "https://sik.search.blue.cdtapps.com/ca/en/search"
CART = "https://prod.cart.caas.selling.ingka.com/api/v1/ca"
FAVS = "https://favs.oneweb.ingka.com/graphql"
PURCHASES = "https://cssom-prod.ingka.com/purchase-history/graphql"
STORES = "https://www.ikea.com/ca/en/meta-data/informera/stores-detailed.json"
STOCK = "https://api.salesitem.ingka.com/availabilities/ru/ca"


def login(client, expires_in=3600):
    tok = access_token(expires_in=expires_in)
    client.auth.store.save(auth.Session(access_token=tok, expires_at=time.time() + expires_in, country="ca", refresh_token="r"))
    return tok


# --------------------------------------------------------------------------- search


def test_search_builds_request_and_normalizes(router, make_client):
    router.add("POST", SEARCH, load_fixture("search.json"))
    client = make_client(settings=Settings(country="ca", store="101", postal_code="zz99zz"))
    result = client.search("table", filters={"colors": "10139,10156", "new-product": True}, sort="price-asc", limit=10, offset=20)

    req = router.last("POST", "/search")
    assert req.url.params["c"] == "sr"
    assert req.headers["content-type"].startswith("text/plain")
    body = json.loads(req.content)
    assert body["searchParameters"] == {"input": "table", "type": "QUERY"}
    assert body["store"] == "101"
    assert body["zip"] == "ZZ99ZZ"
    comp = body["components"][0]
    assert comp["window"] == {"size": 10, "offset": 20}
    assert comp["sort"] == "PRICE_LOW_TO_HIGH"
    assert comp["filterConfig"]["f-colors"] == ["10139", "10156"]
    assert comp["filterConfig"]["f-new-product"] is True

    assert result["total"] == 42
    assert result["count"] == 2
    assert result["sort"] == "PRICE_LOW_TO_HIGH"
    first = result["items"][0]
    assert first["item_no"] == "12345678"
    assert first["price"] == 29.99
    assert first["category"] == {"id": "10001", "name": "Side tables"}
    assert result["items"][1]["item_type"] == "SPR"
    filters = {f["parameter"]: f for f in result["filters"]}
    assert filters["f-colors"]["values"][1]["selected"] is True
    assert filters["f-measurement-buckets"]["values"][0]["name"] == "Width: 0 - 49 cm"
    assert filters["f-new-product"]["type"] == "BOOLEAN"


@pytest.mark.parametrize(
    ("given", "sent"), [("price-desc", "PRICE_HIGH_TO_LOW"), ("MOST_POPULAR", "MOST_POPULAR"), ("best_new", "BEST_NEW")]
)
def test_search_accepts_aliases_and_raw_sort_ids(router, make_client, given, sent):
    router.add("POST", SEARCH, load_fixture("search.json"))
    make_client().search("x", sort=given)
    assert json.loads(router.last().content)["components"][0]["sort"] == sent


def test_search_category_browse(router, make_client):
    router.add("POST", SEARCH, load_fixture("search.json"))
    result = make_client().search(category="st001")
    assert json.loads(router.last().content)["searchParameters"] == {"input": "st001", "type": "CATEGORY"}
    assert result["category"] == "st001"


def test_search_text_within_category_uses_server_filter(router, make_client):
    router.add("POST", SEARCH, load_fixture("search.json"))
    result = make_client().search("chair", category="10000", offset=24)
    body = json.loads(router.last().content)
    assert body["searchParameters"] == {"input": "chair", "type": "QUERY"}
    assert body["components"][0]["filterConfig"]["f-subcategories"] == ["10000"]
    assert body["components"][0]["window"]["offset"] == 24
    assert result["total"] == 42  # the server's total, not a client-side count
    assert result["query"] == "chair" and result["category"] == "10000"


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"query": "x", "limit": 0}, {"query": "x", "limit": 101}, {"query": "x", "offset": -1}, {"query": "x", "sort": "price low"}],
)
def test_search_validation(make_client, kwargs):
    query = kwargs.pop("query", None)
    with pytest.raises(UsageError):
        make_client().search(query, **kwargs)


def test_suggest(router, make_client):
    router.add(
        "GET",
        "https://sik.search.blue.cdtapps.com/ca/en/search-box",
        {
            "searchBox": {
                "universal": [
                    {"completion": {"text": "kallax"}},
                    {"completion": {"text": "kallax shelf"}},
                    {"completion": {"text": "kallax"}},
                ]
            }
        },
    )
    assert make_client().suggest("kal") == ["kallax", "kallax shelf"]
    assert router.last().url.params["q"] == "kal"


def test_categories_are_parsed_and_cached(router, make_client):
    router.add("GET", "https://www.ikea.com/ca/en/cat/products-products/", load_fixture("categories_page.html"))
    client = make_client()
    cats = client.categories()
    assert {c["id"]: c["name"] for c in cats} == {"st001": "Storage furniture", "10653": "Rugs", "34204": "Outdoor & rugs"}
    assert [c["id"] for c in client.categories("rug")] == ["10653", "34204"]
    assert len(router.requests) == 1


# --------------------------------------------------------------------------- products


def test_product_details_from_page(router, make_client):
    router.add("GET", "https://www.ikea.com/ca/en/products/678/12345678.json", load_fixture("product_summary.json"))
    router.add("GET", "https://www.ikea.com/ca/en/p/testbord-side-table-white-12345678/", load_fixture("product_page.html"))
    p = make_client().product("123.456.78")
    assert p["details_available"] is True
    assert p["name"] == "TESTBORD"
    assert p["formatted_item_no"] == "123.456.78"
    assert p["dimensions"] == [{"name": "Width", "value": "55 cm"}, {"name": "Height", "value": "45 cm"}]
    assert p["packages"][0]["weight"] == "3.5 kg"
    assert p["materials"] == ["Top: Particleboard"]
    assert p["care"] == ["Wipe clean."]
    assert p["designer"] == "Test Designer"
    assert p["variants"][0]["options"][1] == {
        "name": "black",
        "item_no": "22345678",
        "selected": False,
        "url": "https://www.ikea.com/ca/en/p/x-22345678/",
    }
    assert p["images"] == ["https://www.ikea.com/images/a.jpg", "https://www.ikea.com/images/b.jpg"]
    assert p["secondary_ratings"] == {"product_quality": 4.4}


def test_product_falls_back_to_combination(router, make_client):
    router.add("GET", "https://www.ikea.com/ca/en/products/678/12345678.json", "Not Found", status=404)
    router.add("GET", "https://www.ikea.com/ca/en/products/678/s12345678.json", load_fixture("product_summary.json"))
    p = make_client().product_summary("12345678")
    assert p["item_type"] == "SPR"


def test_product_from_url_and_not_found(router, make_client):
    router.add("GET", "https://www.ikea.com/ca/en/products/678/s12345678.json", "Not Found", status=404)
    router.add("GET", "https://www.ikea.com/ca/en/products/678/12345678.json", "Not Found", status=404)
    with pytest.raises(NotFound):
        make_client().product_summary("https://www.ikea.com/ca/en/p/testbord-s12345678/")
    assert router.requests[0].url.path.endswith("/s12345678.json")


def test_product_page_without_data_returns_summary(router, make_client):
    router.add("GET", "https://www.ikea.com/ca/en/products/678/12345678.json", load_fixture("product_summary.json"))
    router.add("GET", "https://www.ikea.com/ca/en/p/testbord-side-table-white-12345678/", "<html>changed</html>")
    p = make_client().product("12345678")
    assert p["details_available"] is False
    assert p["price"] == 29.99


# --------------------------------------------------------------------------- stores and stock


def test_stores_cached_and_hidden_filtered(router, make_client):
    router.add("GET", STORES, load_fixture("stores.json"))
    client = make_client()
    stores = client.stores()
    assert [s["id"] for s in stores] == ["202", "101"]
    assert stores[1]["province"] == "QQ"
    client.stores()
    assert len(router.requests) == 1
    assert client.find_store("testville")["id"] == "101"
    assert client.find_store("202")["name"] == "IKEA Sampleton"
    with pytest.raises(NotFound):
        client.find_store("nowhere")
    with pytest.raises(UsageError):
        client.find_store("IKEA")


def test_stock(router, make_client):
    router.add("GET", STORES, load_fixture("stores.json"))
    router.add("GET", STOCK, load_fixture("availability.json"))
    result = make_client().stock(["123.456.78", "11111111"])
    req = router.last("GET", "/availabilities")
    assert req.url.params["itemNos"] == "12345678,11111111"
    assert "Restocks" in req.url.params["expand"]
    assert req.headers["x-client-id"]
    item = result[0]
    assert item["home_delivery"]["status"] == "in stock"
    stores = {s["store_id"]: s for s in item["stores"]}
    assert set(stores) == {"101", "202"}  # unit 9999 without stock data is skipped
    assert stores["101"]["quantity"] == 12
    assert stores["101"]["locations"] == [{"division": "SELF_SERVE", "aisle": "7", "bin": "12"}]
    assert stores["202"]["status"] == "out of stock"
    assert stores["202"]["restocks"][0] == {"earliest": "2026-02-01", "latest": "2026-02-03", "quantity": 20, "reliability": "HIGH"}
    assert stores["202"]["locations"][0]["department"] == "Tables"
    assert result[1] == {"item_no": "11111111", "item_type": None, "home_delivery": None, "stores": [], "error": "not sold here"}


def test_stock_single_store(router, make_client):
    router.add("GET", STORES, load_fixture("stores.json"))
    router.add("GET", STOCK, load_fixture("availability.json"))
    result = make_client().stock(["12345678"], store="202")
    assert [s["store_id"] for s in result[0]["stores"]] == ["202"]


def test_stock_validation(make_client):
    with pytest.raises(UsageError):
        make_client().stock([])
    with pytest.raises(UsageError):
        make_client().stock([str(10000000 + i) for i in range(51)])
    with pytest.raises(UsageError):
        make_client().stock(["123"])


# --------------------------------------------------------------------------- delivery


def test_delivery(router, make_client):
    add_guest(router)
    router.add(
        "POST", "https://api.ingka.ikea.com/address/location", {"postCode": "ZZ99ZZ", "matches": [{"stateCode": "QQ", "city": "Testville"}]}
    )
    router.add("POST", "https://web-api.ikea.com/cart-bff-cf/api/v2/fulfilment", load_fixture("fulfilment.json"))
    result = make_client().delivery([("12345678", 2)], postal_code="zz9 9zz", store="101")
    body = router.body("POST", "/fulfilment")
    assert body["address"] == {"postalCode": "ZZ99ZZ", "stateCode": "QQ", "storeId": "101", "country": "ca", "language": "en"}
    assert body["items"] == [{"itemNo": "12345678", "quantity": 2, "type": "ART", "childItems": []}]
    assert body["consumerName"] == "Web"
    assert router.last("POST", "/fulfilment").headers["authorization"].startswith("Bearer ")
    assert result["home_delivery"]["price"] == 19.99
    assert result["home_delivery"]["original_price"] == 29.99
    assert result["click_and_collect"]["lead_time_days"] == "1 - 2"
    item = result["items"][0]
    assert item["in_store"] is False
    assert item["in_store_restock"] == {"earliestDate": "2026-02-01", "latestDate": "2026-02-03"}
    assert result["province"] == "QQ"


def test_delivery_needs_postal_code(make_client):
    with pytest.raises(UsageError):
        make_client().delivery([("12345678", 1)])


# --------------------------------------------------------------------------- cart


def test_cart_show_as_guest(router, make_client):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    cart = make_client().cart()
    req = router.last("GET", "/api/v1/ca")
    assert req.headers["x-consumer-name"] == "Web Cart"
    assert req.url.params["fetchItemInfo"] == "true"
    assert cart["guest"] is True
    assert cart["lines"] == 2
    assert cart["quantity"] == 3
    assert cart["total"] == 108.98
    assert cart["discount_codes"] == ["SAVE10"]
    assert cart["items"][0] == {
        "item_no": "12345678",
        "item_type": "ART",
        "name": "TESTBORD",
        "type_name": "side table",
        "description": "white",
        "measure": "55x55 cm",
        "quantity": 2,
        "unit_price": 29.99,
        "subtotal": 59.98,
        "family_price": False,
        "packages": 1,
        "children": [],
    }


def test_cart_uses_account_when_logged_in(router, make_client):
    router.add("GET", CART, load_fixture("cart.json"))
    client = make_client()
    tok = login(client)
    cart = client.cart()
    assert cart["guest"] is False
    assert router.last().headers["authorization"] == f"Bearer {tok}"
    assert not [r for r in router.requests if r.url.path == "/guest/token"]


def test_cart_add_merges_duplicates(router, make_client):
    add_guest(router)
    router.add("POST", CART + "/items", {"cartId": "x", "cart": load_fixture("cart.json")}, status=201)
    cart = make_client().cart_add([("123.456.78", 1), ("12345678", 2), ("87654321", 1)])
    body = router.body("POST", "/items")
    assert body["items"] == [{"itemNo": "12345678", "quantity": 3}, {"itemNo": "87654321", "quantity": 1}]
    assert body["shoppingProfile"] == "ONLINE"
    assert body["group"] == "DEFAULT"
    assert body["populateCart"]["fetchItemInfo"] is True
    assert cart["lines"] == 2


@pytest.mark.parametrize("qty", [0, -1, 1000, "two"])
def test_cart_add_rejects_bad_quantities(make_client, qty):
    with pytest.raises(UsageError):
        make_client().cart_add([("12345678", qty)])


def test_cart_set_and_zero_removes(router, make_client):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    router.add("PUT", CART + "/items", {"cart": load_fixture("cart.json")})
    router.add("DELETE", CART + "/items", {"cart": load_fixture("cart_empty.json")})
    make_client().cart_set([("12345678", 5), ("87654321", 0)])
    assert router.body("PUT", "/items")["items"] == [{"itemNo": "12345678", "quantity": 5}]
    assert router.last("DELETE", "/items").url.params["itemNumbers"] == "87654321"


def test_cart_set_requires_item_in_cart(router, make_client):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    with pytest.raises(NotFound):
        make_client().cart_set([("11111111", 2)])


def test_cart_remove_clear_and_coupons(router, make_client):
    add_guest(router)
    router.add("DELETE", CART + "/items", {"cart": load_fixture("cart.json")})
    router.add("DELETE", CART + "/groups/DEFAULT", {"cartId": "x", "cart": None})
    router.add("GET", CART, load_fixture("cart.json"))
    router.add("POST", CART + "/discount-codes", {"cart": load_fixture("cart.json")})
    router.add("DELETE", CART + "/discount-codes", {"cart": load_fixture("cart.json")})
    client = make_client()
    client.cart_remove(["12345678", "123.456.78", "87654321"])
    assert router.last("DELETE", "/items").url.params["itemNumbers"] == "12345678,87654321"
    router.add("GET", CART, load_fixture("cart_empty.json"))
    cleared = client.cart_clear()
    assert cleared["empty"] is True
    client.cart_add_coupon(" SAVE10 ")
    assert router.body("POST", "/discount-codes")["discountCodes"] == ["SAVE10"]
    client.cart_remove_coupon("SAVE10")
    assert router.last("DELETE", "/discount-codes").url.params["discountCodes"] == "SAVE10"


def test_cart_clear_on_missing_cart(router, make_client):
    add_guest(router)
    router.add("DELETE", CART + "/groups/DEFAULT", {"errors": [{"code": "CART_NOT_FOUND", "description": "No cart found"}]}, status=404)
    router.add("GET", CART, load_fixture("cart_empty.json"))
    assert make_client().cart_clear()["empty"] is True


def test_cart_api_errors_are_readable(router, make_client):
    add_guest(router)
    router.add(
        "POST",
        CART + "/discount-codes",
        {
            "title": "BadRequest",
            "errors": [{"code": "INVALID_DISCOUNT_CODE", "description": "NOPE is not valid"}],
            "items": [],
            "fields": [],
        },
        status=400,
    )
    router.add(
        "POST",
        CART + "/items",
        {"title": "BadRequest", "errors": [], "items": [{"itemNo": "00000000", "description": "Item is invalid"}], "fields": []},
        status=400,
    )
    client = make_client()
    with pytest.raises(ApiError) as exc:
        client.cart_add_coupon("NOPE")
    assert "NOPE is not valid" in exc.value.message
    assert exc.value.details == {"codes": ["INVALID_DISCOUNT_CODE"]}
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        client.cart_add([("00000000", 1)])
    assert "00000000: Item is invalid" in exc.value.message


# --------------------------------------------------------------------------- favourites


def test_lists_and_resolution(router, make_client):
    add_guest(router)
    router.add("POST", FAVS, load_fixture("favs_lists.json"))
    client = make_client()
    lists = client.lists()
    assert [x["name"] for x in lists] == ["Living room", "Kitchen"]
    assert lists[0]["items"][0]["unit_price"] == 9.99
    req = router.last("POST", "/graphql")
    assert req.headers["x-client-id"]
    assert json.loads(req.content)["variables"]["languageCode"] == "en"
    assert client.resolve_list("kitchen") == "list-b"
    assert client.resolve_list("list-a") == "list-a"
    assert client.resolve_list(None) is None
    with pytest.raises(NotFound):
        client.resolve_list("garage")


def test_list_add_sends_items_and_list_id(router, make_client):
    add_guest(router)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "addItems" in payload["query"]:
            return httpx.Response(200, json={"data": {"list": load_fixture("favs_lists.json")["data"]["lists"][1]}})
        return httpx.Response(200, json=load_fixture("favs_lists.json"))

    router.add("POST", FAVS, handler)
    make_client().list_add([("12345678", 2)], list_ref="Kitchen")
    body = router.body("POST", "/graphql")
    assert body["variables"]["items"] == [{"itemNo": "12345678", "quantity": 2}]
    assert body["variables"]["listId"] == "list-b"
    assert body["variables"]["ignoreUnavailable"] is False


def test_favourites_graphql_errors(router, make_client):
    add_guest(router)
    router.add("POST", FAVS, {"errors": [{"message": "list not found", "extensions": {"code": "LIST_NOT_FOUND"}}], "data": None})
    with pytest.raises(NotFound):
        make_client().get_list()
    router.add("POST", FAVS, {"errors": [{"message": "Could not add", "extensions": {"code": "INVALID_ITEM_NUMBER"}}], "data": None})
    with pytest.raises(ApiError) as exc:
        make_client().list_add([("00000000", 1)])
    assert exc.value.details == {"codes": ["INVALID_ITEM_NUMBER"]}


# --------------------------------------------------------------------------- orders


def test_orders_need_login(make_client):
    with pytest.raises(AuthRequired):
        make_client().orders()


def test_orders_list(router, make_client):
    router.add("POST", PURCHASES, load_fixture("history.json"))
    client = make_client()
    login(client)
    data = client.orders(limit=5)
    req = router.last("POST", "/graphql")
    assert json.loads(req.content)["variables"] == {"skip": 0, "take": 5}
    assert req.headers["x-cssom-build"] == "standalone"
    assert data["total"] == 2
    assert data["purchases"][0]["date"] == "2026-01-15T12:30:00Z"
    assert data["purchases"][1]["type"] == "receipt"
    second = client.orders(limit=1, offset=1)
    assert json.loads(router.last().content)["variables"] == {"skip": 0, "take": 2}
    assert [p["id"] for p in second["purchases"]] == ["222222222"]
    with pytest.raises(UsageError):
        client.orders(limit=0)


def test_order_detail_and_status(router, make_client):
    router.add("POST", PURCHASES, load_fixture("order.json"))
    client = make_client()
    login(client)
    d = client.order("111111111")
    assert d["type"] == "order"
    assert d["costs"]["total"]["value"] == 67.77
    assert d["items"][0]["total_price"] == "$59.98"
    dl = d["deliveries"][0]
    assert dl["status"] == "IN_TRANSIT"
    assert dl["estimated_from"] == "2026-01-20T08:00:00Z"
    s = client.order_status("111111111")
    assert s["deliveries"][0]["timeline"][-1] == {"status": "DELIVERED", "tense": "FUTURE"}


def test_order_falls_back_to_receipt(router, make_client):
    receipt = load_fixture("order.json")["data"]["order"]

    def handler(request: httpx.Request) -> httpx.Response:
        if "receipt(" in json.loads(request.content)["query"]:
            return httpx.Response(200, json={"data": {"receipt": receipt}})
        return httpx.Response(
            200, json={"errors": [{"message": "Purchase not found", "extensions": {"errorCode": 404}}], "data": {"order": None}}
        )

    router.add("POST", PURCHASES, handler)
    client = make_client()
    login(client)
    assert client.order("222222222")["type"] == "receipt"


def test_order_not_found_and_bad_number(router, make_client):
    router.add(
        "POST", PURCHASES, {"errors": [{"message": "Purchase not found", "extensions": {"errorCode": 404}}], "data": {"order": None}}
    )
    client = make_client()
    login(client)
    with pytest.raises(NotFound):
        client.order("999999999")
    with pytest.raises(UsageError):
        client.order("12; drop")


# --------------------------------------------------------------------------- loyalty


def test_loyalty(router, make_client):
    router.add("GET", "https://api.wlo.ingka.com/loyalty/v1/ca/customer-data", load_fixture("customer_data.json"))
    router.add("GET", "https://api.wlo.ingka.com/loyalty/v1/token-balance", {"tokenBalance": 75})
    router.add("GET", STORES, load_fixture("stores.json"))
    client = make_client()
    login(client)
    d = client.loyalty()
    assert d["member"] is True
    assert d["points"] == 75
    assert d["card_number"].endswith("1234")
    assert "000000000000" not in d["card_number"]
    assert d["preferred_store"] == {"id": "101", "name": "IKEA Testville"}


def test_rewards(router, make_client):
    base = "https://www.ikea.com/ca/en/lower-funnel-fragments/api/v1"
    router.add("POST", base + "/rewards-service/balance/", {"keysBalance": 75})
    router.add("POST", base + "/rewards-service/offers/", load_fixture("reward_offers.json"))
    router.add(
        "GET",
        "https://api.rew.ingka.com/rew/v3/rewards",
        {"items": [{"content": {"name": "$5 off"}, "status": "ACTIVE", "validTo": "2026-02-01"}, {"content": None, "id": "x"}, "junk"]},
    )
    client = make_client()
    login(client)
    d = client.rewards()
    assert d["points"] == 75
    assert [o["points"] for o in d["offers"]] == [50, 100]
    assert d["offers"][1]["description"] == "Get $10 off."
    assert router.last("GET", "/rew/v3/rewards").headers["api-version"] == "2020-11-11"
    assert d["claimed"] == [
        {"name": "$5 off", "status": "ACTIVE", "valid_to": "2026-02-01"},
        {"name": None, "status": None, "valid_to": None},
    ]


# --------------------------------------------------------------------------- transport


def test_network_errors_are_wrapped(make_client, router):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    router.add("POST", SEARCH, boom)
    with pytest.raises(NetworkError):
        make_client().search("x")


def test_non_json_error_bodies(router, make_client):
    router.add("POST", SEARCH, "<html>Bad gateway</html>", status=502)
    with pytest.raises(ApiError) as exc:
        make_client().search("x")
    assert exc.value.status == 502
    assert "<html" not in exc.value.message


# --------------------------------------------------------------------------- review round: 401 handling and removals


def _unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"title": "ApiUnauthorizedError", "errors": [{"code": "UNAUTHORIZED", "description": "Invalid token"}]})


def test_401_on_read_refreshes_once_and_retries(router, make_client):
    client = make_client()
    old = login(client)
    new = access_token(expires_in=86400, jti="new")
    router.add("POST", "https://ca.accounts.ikea.com/oauth/token", {"access_token": new, "refresh_token": "r2"})
    router.add(
        "GET",
        CART,
        lambda r: httpx.Response(200, json=load_fixture("cart.json")) if r.headers["authorization"] == f"Bearer {new}" else _unauthorized(),
    )
    cart = client.cart()
    assert cart["lines"] == 2
    assert [r.headers["authorization"] for r in router.requests if r.method == "GET"] == [f"Bearer {old}", f"Bearer {new}"]
    assert client.auth.store.load("ca").refresh_token == "r2"


def test_401_after_refresh_means_session_expired(router, make_client):
    client = make_client()
    login(client)
    router.add("POST", "https://ca.accounts.ikea.com/oauth/token", {"access_token": access_token(jti="n"), "refresh_token": "r2"})
    router.add("GET", CART, lambda r: _unauthorized())
    with pytest.raises(SessionExpired) as exc:
        client.cart()
    assert exc.value.exit_code == 3
    assert "ikea auth login" in exc.value.hint


def test_401_on_write_does_not_retry(router, make_client):
    client = make_client()
    login(client)
    router.add("POST", CART + "/items", lambda r: _unauthorized())
    with pytest.raises(SessionExpired):
        client.cart_add([("12345678", 1)])
    assert not [r for r in router.requests if r.url.path == "/oauth/token"]
    assert len([r for r in router.requests if r.url.path.endswith("/items")]) == 1


def test_401_with_environment_token(router, make_client, monkeypatch):
    monkeypatch.setenv("IKEA_ACCESS_TOKEN", access_token())
    router.add("GET", "https://api.wlo.ingka.com/loyalty/v1/token-balance", lambda r: _unauthorized())
    router.add("GET", "https://api.wlo.ingka.com/loyalty/v1/ca/customer-data", lambda r: _unauthorized())
    with pytest.raises(SessionExpired):
        make_client().loyalty()


def test_401_with_guest_token_forgets_it(router, make_client, isolated_env):
    add_guest(router)
    router.add("GET", CART, lambda r: _unauthorized())
    with pytest.raises(ApiError) as exc:
        make_client().cart()
    assert exc.value.status == 401
    assert "run the command again" in exc.value.hint
    assert not (isolated_env / "state" / "guest.json").exists()


def test_cart_remove_rejects_items_not_in_cart(router, make_client):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    with pytest.raises(NotFound) as exc:
        make_client().cart_remove(["12345678", "40631638"])
    assert "406.316.38" in exc.value.message
    assert not [r for r in router.requests if r.method == "DELETE"]


def test_cart_set_zero_for_missing_item_changes_nothing(router, make_client):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    with pytest.raises(NotFound):
        make_client().cart_set([("12345678", 3), ("40631638", 0)])
    assert not [r for r in router.requests if r.method in ("PUT", "DELETE")]
