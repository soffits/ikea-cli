from __future__ import annotations

import json
import os
import stat

import pytest

from ikea_cli import normalize
from ikea_cli.config import Paths, Settings, load_settings, save_config, write_private_json
from ikea_cli.errors import ConfigError, UsageError


def test_paths_follow_ikea_cli_home(isolated_env):
    paths = Paths.default()
    assert paths.config_file == isolated_env / "config" / "config.json"
    assert paths.state_dir == isolated_env / "state"


def test_settings_validation():
    s = Settings(country="CA", language="EN", store=149, postal_code=" m5v   3l9 ")
    assert (s.country, s.language, s.store, s.postal_code) == ("ca", "en", "149", "M5V 3L9")
    assert s.locale == "en-CA"
    assert s.verified
    assert not Settings(country="us", language="en").verified
    for bad in ({"country": "canada"}, {"language": "e"}, {"store": "north york"}):
        with pytest.raises(ConfigError):
            Settings(**bad)


def test_load_settings_precedence(isolated_env, monkeypatch):
    paths = Paths.default()
    save_config(paths, {"country": "ca", "language": "en", "store": "101", "unknown_key": 1})
    assert load_settings(paths).store == "101"
    monkeypatch.setenv("IKEA_STORE", "202")
    assert load_settings(paths).store == "202"
    assert load_settings(paths, {"store": "303", "language": None}).store == "303"
    assert load_settings(paths).extra == {"unknown_key": 1}


def test_bad_config_file(isolated_env):
    paths = Paths.default()
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("[1, 2]")
    with pytest.raises(ConfigError):
        load_settings(paths)


def test_write_private_json(tmp_path):
    target = tmp_path / "deep" / "file.json"
    write_private_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    write_private_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("30449908", ("30449908", None)),
        ("304.499.08", ("30449908", None)),
        (" 304 499 08 ", ("30449908", None)),
        ("s09563608", ("09563608", "SPR")),
        ("S095.636.08", ("09563608", "SPR")),
        ("https://www.ikea.com/ca/en/p/lack-side-table-white-30449908/", ("30449908", "ART")),
        ("https://www.ikea.com/ca/en/p/lagkapten-spaend-desk-white-s09563608/?foo=1#x", ("09563608", "SPR")),
        ("www.ikea.com/ca/en/p/lack-30449908", ("30449908", "ART")),
    ],
)
def test_parse_item_ref(ref, expected):
    assert normalize.parse_item_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "1234567", "123456789", "lamp", "https://www.ikea.com/ca/en/cat/rugs-10653/"])
def test_parse_item_ref_rejects(ref):
    with pytest.raises(UsageError):
        normalize.parse_item_ref(ref)


def test_format_item_no():
    assert normalize.format_item_no("30449908") == "304.499.08"
    assert normalize.format_item_no("abc") == "abc"


def test_mask():
    assert normalize.mask("1234567890") == "••••••7890"
    assert normalize.mask(None) is None


def test_cart_normalizer_handles_wrapped_and_null_cart():
    assert normalize.cart({"cartId": "x", "cart": None})["empty"] is True
    assert normalize.cart({})["items"] == []


def test_price_prefers_incl_tax():
    assert normalize._price_pair({"inclTax": 11.3, "exclTax": 10.0}) == 11.3
    assert normalize._price_pair({"inclTax": None, "exclTax": 10.0}) == 10.0
    assert normalize._price_pair(None) is None
