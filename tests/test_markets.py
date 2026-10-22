from __future__ import annotations

import json

import httpx
import pytest
from conftest import load_fixture

from ikea_cli import cli, markets, normalize
from ikea_cli.client import IkeaClient
from ikea_cli.config import Paths, Settings, load_settings, save_config, write_private_json
from ikea_cli.errors import EXIT_CONFIG, ConfigError, MarketNotSet, UnsupportedMarket
from ikea_cli.markets import PriceFormat

TABLE = {
    "generated": "2026-01-01",
    "markets": {
        "aa": {
            "name": "Alphaland",
            "keywords": ["Alphaland", "Alfaland"],
            "languages": {"en": "English", "xx": "Ex"},
            "default_language": "xx",
            "currency": "AAA",
            "price_format": {
                "en": {"prefix": "A$", "suffix": "", "decimals": 2, "group": ",", "decimal": "."},
                "xx": {"prefix": "", "suffix": " A", "decimals": 2, "group": " ", "decimal": ","},
            },
            "auth": {"domain": "aa.accounts.example", "client_id": "client-aa"},
        },
        "ee": {"name": "Epsilonland", "languages": {"en": "English"}, "default_language": "en", "currency": None},
        "bb": {
            "name": "Betaland",
            "keywords": ["Betaland"],
            "languages": {"en": "English"},
            "default_language": "en",
            "currency": "BBB",
            "price_format": {"en": {"prefix": "", "suffix": "", "decimals": 0, "group": ".", "decimal": ","}},
            "auth": None,
        },
    },
    "other_sites": {
        "cc": {"name": "Gammaland", "url": "https://www.ikea.example/"},
        "dd": {"name": "Deltaland", "url": "https://www.ikea.com/aa/en/", "served_by": "aa"},
    },
}


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setattr(markets, "_table", lambda: TABLE)
    markets.all_markets.cache_clear()
    yield TABLE
    markets.all_markets.cache_clear()


# --------------------------------------------------------------------------- shipped table


def test_shipped_table_is_consistent():
    found = markets.all_markets()
    assert len(found) >= 40
    for cc, m in found.items():
        assert len(cc) == 2 and m.name and m.languages
        assert m.default_language in m.languages
        assert m.currency is None or len(m.currency) == 3
        assert m.has_shop == bool(m.price_formats)
        assert (m.auth_domain is None) == (m.auth_client_id is None)
    for cc in markets.other_sites():
        assert cc not in found
    assert found["ca"].auth_domain == "ca.accounts.ikea.com"
    assert "cn" in markets.other_sites()


# --------------------------------------------------------------------------- resolving


def test_resolve_market_and_default_language(table):
    market, lang = markets.resolve("AA")
    assert (market.name, lang) == ("Alphaland", "xx")
    assert markets.resolve("aa", "en")[1] == "en"


def test_resolve_explains_unsupported(table):
    with pytest.raises(UnsupportedMarket, match="runs its own website"):
        markets.resolve("cc")
    with pytest.raises(UnsupportedMarket, match="served by the IKEA Alphaland website") as exc:
        markets.resolve("dd")
    assert "country aa" in (exc.value.hint or "")
    with pytest.raises(UnsupportedMarket, match="not an IKEA market"):
        markets.resolve("zz")
    with pytest.raises(UnsupportedMarket, match="not available in 'fr'"):
        markets.resolve("aa", "fr")
    with pytest.raises(UnsupportedMarket, match="lists no products online"):
        markets.resolve("ee")


def test_search_by_name_or_keyword(table):
    assert [m.country for m in markets.search("alfa")] == ["aa"]
    assert [m.country for m in markets.search("bb")] == ["bb"]
    assert len(markets.search()) == 3


# --------------------------------------------------------------------------- prices


@pytest.mark.parametrize(
    ("fmt", "amount", "currency", "expected"),
    [
        (PriceFormat("$", "", 2, ",", "."), 1299, "CAD", "$1,299.00"),
        (PriceFormat("", "$", 2, "\u00a0", ","), 1299.5, "CAD", "1\u00a0299,50$"),
        (PriceFormat("¥", "", 0, ",", "."), 12990, "JPY", "¥12,990"),
        (PriceFormat("¥", "", 0, ",", "."), 453.64, "JPY", "¥453.64"),
        (PriceFormat("CHF", "", 2, "'", "."), 3540.25, "CHF", "CHF 3'540.25"),
        (PriceFormat("", "", 2, " ", ","), 13045.5, "PLN", "13 045,50 PLN"),
        (PriceFormat("", ":-", 0, " ", "."), 47570, "SEK", "47 570:-"),
        (PriceFormat("", "", 2, "", "."), -5, None, "-5.00"),
    ],
)
def test_price_format(fmt, amount, currency, expected):
    assert fmt.format(amount, currency) == expected


def test_client_formats_prices_per_language(table, isolated_env):
    with IkeaClient(Settings(country="aa"), paths=Paths.default()) as client:
        assert client.format_price(1234.5) == "1 234,50 A"
        assert client.format_price(3, "ZZZ") == "3.00 ZZZ"  # a foreign currency is not given this market's symbol
        assert client.format_price(None) == "-"
    with IkeaClient(Settings(country="aa", language="en"), paths=Paths.default()) as client:
        assert client.format_price(1234.5) == "A$1,234.50"


# --------------------------------------------------------------------------- settings


def test_settings_need_a_market(table):
    s = Settings()
    assert s.country is None
    with pytest.raises(MarketNotSet):
        s.market()
    with pytest.raises(MarketNotSet):
        IkeaClient(s)
    assert Settings(country="aa").language == "xx"
    with pytest.raises(UnsupportedMarket):
        Settings(country="cc").market()


def test_market_without_public_login(table):
    with pytest.raises(ConfigError, match="not available for IKEA Betaland"):
        Settings(country="bb").auth_market()
    assert Settings(country="aa").auth_market().client_id == "client-aa"


def test_new_user_gets_no_default_market(isolated_env, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    s = load_settings(Paths.default())
    assert s.country is None and not s.implicit_country


def test_earlier_setup_keeps_canada(isolated_env, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    paths = Paths.default()
    save_config(paths, {"store": "101"})
    s = load_settings(paths)
    assert (s.country, s.language, s.implicit_country) == ("ca", "en", True)


def test_earlier_guest_session_keeps_canada(isolated_env, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    paths = Paths.default()
    write_private_json(paths.guest_file, {"access_token": "x", "expires_at": 0, "country": "ca"})
    assert load_settings(paths).country == "ca"
    write_private_json(paths.guest_file, {"access_token": "x", "expires_at": 0, "country": "de"})
    assert load_settings(paths).country is None


# --------------------------------------------------------------------------- stores and postal codes


def test_store_region_drops_country_prefix():
    raw = load_fixture("stores.json")[0]
    assert normalize.store({**raw, "address": {"stateProvinceCode": "GBABE"}}, "gb")["province"] == "ABE"
    assert normalize.store({**raw, "address": {"stateProvinceCode": "JP04"}}, "jp")["province"] == "04"
    assert normalize.store({**raw, "address": {"stateProvinceCode": "W"}}, "se")["province"] == "W"


def test_delivery_without_postal_lookup(router, make_client):
    router.add(
        "POST",
        "https://api.ingka.ikea.com/address/location",
        {"errorCode": "BAD_REQUEST", "errorMessage": "This country code is not supported for location lookup"},
        status=400,
    )
    router.add("POST", "https://api.ingka.ikea.com/guest/token", {"access_token": "guest", "expires_in": "720h"})
    router.add("POST", "https://web-api.ikea.com/cart-bff-cf/api/v2/fulfilment", load_fixture("fulfilment.json"))
    result = make_client().delivery([("12345678", 1)], postal_code="99999")
    body = json.loads(router.last("POST", "/fulfilment").content)
    assert body["address"]["postalCode"] == "99999" and body["address"]["stateCode"] is None
    assert result["province"] is None


# --------------------------------------------------------------------------- command line


@pytest.fixture
def run(router, monkeypatch, capsys):
    def factory(settings, **kwargs):
        return IkeaClient(settings, http=httpx.Client(transport=httpx.MockTransport(router)), **kwargs)

    monkeypatch.setattr(cli, "IkeaClient", factory)

    def invoke(*argv: str):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return invoke


def test_first_run_is_told_to_pick_a_market(run, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    code, out, _ = run("--json", "search", "desk")
    assert code == EXIT_CONFIG
    error = json.loads(out)["error"]
    assert error["code"] == "market_not_set" and "ikea markets" in error["hint"]
    code, out, _ = run("config", "show")
    assert code == 0 and "No market set" in out


def test_markets_command(run, table):
    code, out, _ = run("markets")
    assert code == 0 and "Alphaland" in out and "Betaland" in out and "Gammaland" not in out
    code, out, _ = run("--json", "markets", "--all")
    data = json.loads(out)["data"]
    by_code = {m["country"]: m for m in data}
    assert by_code["aa"]["supported"] and by_code["aa"]["languages"] == ["en", "xx"] and by_code["aa"]["login"]
    assert not by_code["bb"]["login"]
    assert by_code["cc"]["supported"] is False and by_code["cc"]["url"] == "https://www.ikea.example/"


def test_config_set_country(run, table, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    paths = Paths.default()
    code, _, err = run("config", "set", "store", "101")
    assert code == EXIT_CONFIG and "Choose your IKEA market" in err
    assert run("config", "set", "country", "aa")[0] == 0
    assert json.loads(paths.config_file.read_text())["language"] == "xx"
    save_config(paths, {**json.loads(paths.config_file.read_text()), "language": "en", "store": "101", "postal_code": "Q1"})
    code, out, _ = run("config", "set", "country", "bb")
    saved = json.loads(paths.config_file.read_text())
    assert code == 0 and saved == {"country": "bb", "language": "en"}
    assert "Removed store" in out
    code, _, err = run("config", "set", "country", "cc")
    assert code == EXIT_CONFIG and "runs its own website" in err
    code, _, err = run("config", "set", "language", "fr")
    assert code == EXIT_CONFIG and "not available in 'fr'" in err


def test_earlier_setup_is_written_down_once(run, router, monkeypatch):
    monkeypatch.delenv("IKEA_COUNTRY")
    paths = Paths.default()
    save_config(paths, {"postal_code": "ZZ9 9ZZ"})
    router.add("GET", "https://www.ikea.com/ca/en/meta-data/informera/stores-detailed.json", load_fixture("stores.json"))
    code, _, err = run("stores")
    assert code == 0 and "'ca' was saved" in err
    assert json.loads(paths.config_file.read_text())["country"] == "ca"
    _, _, err = run("stores")
    assert "was saved" not in err


def test_unsupported_market_from_flag(run):
    code, _, err = run("--country", "cn", "search", "desk")
    assert code == EXIT_CONFIG and "runs its own website" in err
