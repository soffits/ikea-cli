from __future__ import annotations

import io
import json
import time

import httpx
import pytest
from conftest import access_token, add_guest, load_fixture

from ikea_cli import auth, cli, markets
from ikea_cli.client import IkeaClient
from ikea_cli.config import Paths
from ikea_cli.errors import EXIT_API, EXIT_AUTH, EXIT_NOT_FOUND, EXIT_USAGE, UsageError

CART = "https://prod.cart.caas.selling.ingka.com/api/v1/ca"
STORES = "https://www.ikea.com/ca/en/meta-data/informera/stores-detailed.json"


@pytest.fixture
def run(router, monkeypatch, capsys):
    def factory(settings, **kwargs):
        return IkeaClient(settings, http=httpx.Client(transport=httpx.MockTransport(router)), **kwargs)

    monkeypatch.setattr(cli, "IkeaClient", factory)

    def invoke(*argv: str, stdin: str | None = None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return invoke


def test_no_command_prints_help(run):
    code, _out, err = run()
    assert code == EXIT_USAGE
    assert "usage: ikea" in err


def test_usage_error_json_envelope(run):
    code, out, _ = run("--json", "search", "--sort", "price low", "x")
    assert code == EXIT_USAGE
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"]["code"] == "usage"


def test_search_table_and_json(run, router):
    router.add("POST", "https://sik.search.blue.cdtapps.com/ca/en/search", load_fixture("search.json"))
    code, out, _ = run("search", "table", "-n", "2")
    assert code == 0
    assert "123.456.78" in out
    assert "TESTBORD" in out
    assert "$29.99" in out
    assert "next: --page 2" in out
    code, out, _ = run("--json", "search", "table", "--page", "3", "-n", "10", "-f", "colors=10139", "-f", "new-product")
    data = json.loads(out)
    assert data["ok"] is True
    assert data["data"]["items"][0]["item_no"] == "12345678"
    body = router.body("POST", "/search")
    assert body["components"][0]["window"] == {"size": 10, "offset": 20}
    assert body["components"][0]["filterConfig"]["f-colors"] == ["10139"]
    assert body["components"][0]["filterConfig"]["f-new-product"] is True


def test_search_filters_listing(run, router):
    router.add("POST", "https://sik.search.blue.cdtapps.com/ca/en/search", load_fixture("search.json"))
    code, out, _ = run("search", "table", "--filters")
    assert code == 0
    assert "--filter colors=ID" in out
    assert "10139" in out and "black (12) *" in out
    assert "PRICE_LOW_TO_HIGH" in out and "Price: low to high  [price-asc]" in out


def test_global_flags_after_subcommand(run, router):
    router.add("POST", "https://sik.search.blue.cdtapps.com/ca/en/search", load_fixture("search.json"))
    code, out, _ = run("search", "table", "--json")
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_cart_flow_as_guest(run, router):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    router.add("POST", CART + "/items", {"cart": load_fixture("cart.json")}, status=201)
    code, out, _ = run("cart", "show")
    assert code == 0
    assert "Guest cart: 2 lines, 3 items" in out
    assert "Total (before tax): $108.98" in out
    code, out, _ = run("cart", "add", "123.456.78:2", "87654321x3")
    assert code == 0
    assert router.body("POST", "/items")["items"] == [{"itemNo": "12345678", "quantity": 2}, {"itemNo": "87654321", "quantity": 3}]


def test_cart_add_from_file(run, router, tmp_path):
    add_guest(router)
    router.add("POST", CART + "/items", {"cart": load_fixture("cart.json")}, status=201)
    f = tmp_path / "items.txt"
    f.write_text("# shopping\n123.456.78 2\n87654321,1\n\n")
    code, _, _ = run("cart", "add", "--file", str(f))
    assert code == 0
    assert router.body("POST", "/items")["items"] == [{"itemNo": "12345678", "quantity": 2}, {"itemNo": "87654321", "quantity": 1}]
    code, _, _ = run("cart", "add", "--file", "-", stdin='[{"item_no": "12345678", "quantity": 4}]')
    assert code == 0
    assert router.body("POST", "/items")["items"] == [{"itemNo": "12345678", "quantity": 4}]


def test_cart_set_forms(run, router):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    router.add("PUT", CART + "/items", {"cart": load_fixture("cart.json")})
    assert run("cart", "set", "12345678", "4")[0] == 0
    assert router.body("PUT", "/items")["items"] == [{"itemNo": "12345678", "quantity": 4}]
    assert run("cart", "set", "12345678:5")[0] == 0
    code, _, err = run("cart", "set", "12345678", "87654321")
    assert code == EXIT_USAGE
    assert "ITEM:QTY" in err


def test_cart_clear_needs_confirmation(run, router):
    add_guest(router)
    router.add("DELETE", CART + "/groups/DEFAULT", {"cart": None})
    router.add("GET", CART, load_fixture("cart_empty.json"))
    code, _, err = run("cart", "clear", stdin="")
    assert code == EXIT_USAGE
    assert "--yes" in err
    assert not [r for r in router.requests if r.method == "DELETE"]
    code, out, _ = run("cart", "clear", "--yes")
    assert code == 0
    assert "Cart cleared." in out


def test_api_error_exit_code_and_message(run, router):
    add_guest(router)
    router.add(
        "POST", CART + "/discount-codes", {"errors": [{"code": "INVALID_DISCOUNT_CODE", "description": "NOPE is not valid"}]}, status=400
    )
    code, out, err = run("cart", "coupon", "add", "NOPE")
    assert code == EXIT_API
    assert "error: IKEA cart returned HTTP 400: NOPE is not valid" in err
    code, out, _ = run("--json", "cart", "coupon", "add", "NOPE")
    assert json.loads(out)["error"]["details"] == {"codes": ["INVALID_DISCOUNT_CODE"]}


def test_orders_require_login(run):
    code, _, err = run("orders")
    assert code == EXIT_AUTH
    assert "ikea auth login" in err


def test_auth_status_and_logout(run, router):
    code, out, _ = run("auth", "status")
    assert code == 0 and "Not logged in" in out
    store = auth.SessionStore(Paths.default())
    tok = access_token(expires_in=7200)
    store.save(auth.Session(access_token=tok, expires_at=time.time() + 7200, country="ca", refresh_token="r", id_token=None))
    code, out, _ = run("--json", "auth", "status")
    data = json.loads(out)["data"]
    assert data["logged_in"] is True and data["can_refresh"] is True
    assert tok not in out and '"r"' not in out
    router.add("POST", "https://ca.accounts.ikea.com/oauth/revoke", "", status=200)
    code, out, _ = run("auth", "logout")
    assert code == 0 and "Logged out." in out
    assert store.load("ca") is None


def test_login_start_and_finish(run, router, tmp_path):
    code, out, _ = run("--json", "auth", "login", "--start")
    assert code == 0
    started = json.loads(out)["data"]
    assert started["mode"] == "page"
    assert "view-source:" in started["instructions"]
    pending = auth.SessionStore(Paths.default()).load_pending()
    page = load_fixture("authorize_page.html").replace("STATE123", pending["state"])
    tok = access_token()
    router.add("POST", "https://ca.accounts.ikea.com/oauth/token", {"access_token": tok, "refresh_token": "r", "expires_in": 86400})
    code, out, err = run("auth", "login", "--finish", "-", stdin=page)
    assert code == 0, err
    assert "Logged in" in out
    body = router.body("POST", "/oauth/token")
    assert body["code"] == "TESTCODE_abcdefghijklmnop"
    assert body["code_verifier"] == pending["verifier"]
    assert auth.SessionStore(Paths.default()).load_pending() is None
    assert auth.SessionStore(Paths.default()).load("ca").refresh_token == "r"


def test_login_finish_rejects_wrong_state(run):
    run("auth", "login", "--start")
    code, _, err = run("auth", "login", "--finish", "-", stdin=load_fixture("authorize_page.html"))
    assert code == EXIT_AUTH
    assert "different login attempt" in err


def test_login_finish_without_start(run):
    code, _, err = run("auth", "login", "--finish", "-", stdin="ABCDEFGHIJKLMNOPQRST")
    assert code == EXIT_AUTH
    assert "No login is in progress" in err


def test_import_access_token(run):
    tok = access_token(expires_in=3600)
    code, out, _ = run("auth", "import", "--access-token", "-", stdin=f"Bearer {tok}\n")
    assert code == 0
    assert "cannot be refreshed" in out
    assert auth.SessionStore(Paths.default()).load("ca").source == "access-token"
    code, _, _err = run("auth", "import", "--access-token", "-", stdin="nonsense")
    assert code == EXIT_AUTH


def test_import_cookie(run, router):
    router.add("GET", "https://ca.accounts.ikea.com/authorize", lambda r: httpx.Response(200, text=_page_for(r)))
    router.add("POST", "https://ca.accounts.ikea.com/oauth/token", {"access_token": access_token(), "refresh_token": "r"})
    code, _out, err = run("auth", "import", "--cookie", "-", stdin="auth0=s%3Asecret")
    assert code == 0, err
    assert router.last("GET", "/authorize").headers["cookie"] == "auth0=s%3Asecret"
    assert "s%3Asecret" not in json.dumps(json.loads((Paths.default().state_dir / "session-ca.json").read_text()))


def _page_for(request: httpx.Request) -> str:
    state = request.url.params["state"]
    return load_fixture("authorize_page.html").replace("STATE123", state)


def test_config_set_show_unset(run, router):
    router.add("GET", STORES, load_fixture("stores.json"))
    assert run("config", "set", "postal_code", "zz9 9zz")[0] == 0
    assert run("store", "set", "testville")[0] == 0
    code, out, _ = run("--json", "config", "show")
    data = json.loads(out)["data"]
    assert data["postal_code"] == "ZZ9 9ZZ"
    assert data["store"] == "101"
    assert data["verified_market"] is bool(markets.get("ca").tested)
    code, _, _err = run("config", "set", "country", "canada")
    assert code != 0
    assert run("config", "unset", "store")[0] == 0
    assert json.loads(run("--json", "config", "show")[1])["data"]["store"] is None


def test_store_not_found(run, router):
    router.add("GET", STORES, load_fixture("stores.json"))
    code, _, _err = run("store", "set", "atlantis")
    assert code == EXIT_NOT_FOUND


def test_stock_uses_default_store(run, router):
    router.add("GET", STORES, load_fixture("stores.json"))
    router.add("GET", "https://api.salesitem.ingka.com/availabilities/ru/ca", load_fixture("availability.json"))
    run("store", "set", "202")
    code, out, _ = run("stock", "12345678")
    assert code == 0
    assert "IKEA Sampleton" in out and "IKEA Testville" not in out
    assert "20 on 2026-02-01-2026-02-03" in out
    code, out, _ = run("stock", "12345678", "--all")
    assert "IKEA Testville" in out and "aisle 7 bin 12" in out


def test_unverified_market_note(run, router):
    router.add("POST", "https://sik.search.blue.cdtapps.com/it/it/search", load_fixture("search.json"))
    code, _, err = run("--country", "it", "search", "bord")
    assert code == 0
    assert "has not been tested" in err
    router.add("POST", "https://sik.search.blue.cdtapps.com/se/sv/search", load_fixture("search.json"))
    _, _, err = run("--country", "se", "search", "bord")
    assert "has not been tested" not in err


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("30449908", ("30449908", 1)),
        ("304.499.08:2", ("304.499.08", 2)),
        ("30449908x3", ("30449908", 3)),
        ("s09563608*2", ("s09563608", 2)),
    ],
)
def test_parse_item_spec(spec, expected):
    assert cli.parse_item_spec(spec) == expected


def test_parse_item_spec_rejects_garbage():
    with pytest.raises(UsageError):
        cli.parse_item_spec("lamp")


def test_parse_filters():
    assert cli.parse_filters(["colors=1,2", "colors=3", "new-product", "price-buckets=PRICE_0_5000"]) == {
        "colors": ["1", "2", "3"],
        "new-product": True,
        "price-buckets": ["PRICE_0_5000"],
    }
    with pytest.raises(UsageError):
        cli.parse_filters(["=x"])


# --------------------------------------------------------------------------- review round


def test_network_error_exit_code(run, router):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    router.add("POST", "https://sik.search.blue.cdtapps.com/ca/en/search", down)
    code, out, _ = run("--json", "search", "desk")
    assert code == 6
    assert json.loads(out)["error"]["code"] == "network"


def test_unauthorized_session_exits_3(run, router):
    store = auth.SessionStore(Paths.default())
    store.save(auth.Session(access_token=access_token(), expires_at=time.time() + 3600, country="ca"))
    router.add("GET", CART, lambda r: httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED", "description": "Invalid token"}]}))
    code, out, _ = run("--json", "cart")
    assert code == 3
    error = json.loads(out)["error"]
    assert error["code"] == "session_expired"
    assert "ikea auth login" in error["hint"]


def test_import_of_expired_token_keeps_session(run):
    store = auth.SessionStore(Paths.default())
    store.save(auth.Session(access_token=access_token(), expires_at=time.time() + 3600, country="ca", refresh_token="keep"))
    code, _, err = run("auth", "import", "--access-token", "-", stdin=access_token(expires_in=-60))
    assert code == EXIT_AUTH
    assert "expired" in err
    assert store.load("ca").refresh_token == "keep"


def test_logout_keeps_guest_and_can_forget_it(run, router):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart_empty.json"))
    run("cart")
    guest_file = Paths.default().guest_file
    assert guest_file.exists()
    code, out, _ = run("auth", "logout", "--local-only")
    assert code == 0 and guest_file.exists()
    code, out, _ = run("auth", "logout", "--local-only", "--forget-guest")
    assert code == 0 and not guest_file.exists()
    assert "guest cart and lists" in out


def test_cart_remove_unknown_item_fails(run, router):
    add_guest(router)
    router.add("GET", CART, load_fixture("cart.json"))
    code, _, err = run("cart", "remove", "40631638")
    assert code == EXIT_NOT_FOUND
    assert "Not in the cart" in err


@pytest.mark.parametrize(
    "content",
    ['[{"item_no": "12345678", "quantity": "two"}]', '[{"quantity": 2}]', '[{"item_no": "lamp"}]', "[[1, 2]]", '{"item_no": "12345678"}'],
)
def test_bad_items_file_entries_are_usage_errors(run, content):
    code, out, err = run("--json", "cart", "add", "--file", "-", stdin=content)
    assert code == EXIT_USAGE
    assert json.loads(out)["error"]["code"] == "usage"
    assert "Traceback" not in err


def test_unexpected_errors_still_print_an_envelope(run, router, monkeypatch):
    router.add("POST", "https://sik.search.blue.cdtapps.com/ca/en/search", {"results": [{"component": "PRIMARY_AREA", "items": "oops"}]})
    code, out, err = run("--json", "search", "desk")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "error"
    assert "Unexpected error" in error["message"]
    assert "IKEA_DEBUG=1" in error["hint"]
    assert "Traceback" not in err
    monkeypatch.setenv("IKEA_DEBUG", "1")
    code, out, err = run("search", "desk")
    assert code == 1
    assert "Traceback" in err


def test_damaged_caches_are_ignored(run, router, isolated_env):
    router.add("GET", STORES, load_fixture("stores.json"))
    cache = isolated_env / "cache"
    cache.mkdir(parents=True)
    (cache / "stores-ca-en.json").write_text('{"at": "yesterday", "stores": "none"}')
    (cache / "categories-ca-en.json").write_text('{"at": 9e99, "categories": [{"name": "no id"}]}')
    router.add("GET", "https://www.ikea.com/ca/en/cat/products-products/", load_fixture("categories_page.html"))
    assert run("stores")[0] == 0
    assert run("categories", "rug")[0] == 0
