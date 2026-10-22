"""IKEA markets: which countries and languages the website platform serves, and how each one differs.

The table lives in ``markets.json`` next to this file. It is generated from IKEA's own public
pages by ``scripts/update_markets.py`` (country picker, each market's login settings, prices
from a product search), so it can be refreshed when IKEA adds or changes a market.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from .errors import UnsupportedMarket

# Markets whose guest features (search, product, stores, stock, delivery, guest cart and lists,
# login link) were checked against the live website, with the date of the last check.
TESTED: dict[str, str] = {}


@dataclass(frozen=True)
class PriceFormat:
    """How IKEA writes prices in one market and language."""

    prefix: str = ""
    suffix: str = ""
    decimals: int = 2
    group: str = ","
    decimal: str = "."

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PriceFormat:
        if not isinstance(data, dict):
            return cls()
        return cls(
            prefix=str(data.get("prefix") or ""),
            suffix=str(data.get("suffix") or ""),
            decimals=int(data.get("decimals") or 0),
            group=str(data.get("group") or ""),
            decimal=str(data.get("decimal") or "."),
        )

    def format(self, amount: float, currency: str | None = None) -> str:
        decimals = self.decimals
        if decimals == 0 and round(amount) != round(amount, 2):
            decimals = 2  # e.g. a price before tax in a market that shows whole numbers
        whole, _, frac = f"{abs(amount):,.{decimals}f}".partition(".")
        text = whole.replace(",", self.group) + (self.decimal + frac if frac else "")
        prefix, suffix = self.prefix, self.suffix
        if not prefix and not suffix and currency:
            suffix = currency
        if prefix and prefix[-1].isalpha():
            prefix += " "
        if suffix and suffix[0].isalpha():
            suffix = " " + suffix
        return f"{'-' if amount < 0 else ''}{prefix}{text}{suffix}"


@dataclass(frozen=True)
class Market:
    country: str
    name: str
    languages: dict[str, str]
    default_language: str
    currency: str | None = None
    price_formats: dict[str, PriceFormat] = field(default_factory=dict)
    auth_domain: str | None = None
    auth_client_id: str | None = None
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, country: str, data: dict[str, Any]) -> Market:
        auth = data.get("auth") or {}
        languages = {str(k): str(v) for k, v in (data.get("languages") or {}).items()}
        return cls(
            country=country,
            name=str(data.get("name") or country.upper()),
            languages=languages,
            default_language=str(data.get("default_language") or next(iter(languages), "en")),
            currency=data.get("currency"),
            price_formats={str(k): PriceFormat.from_dict(v) for k, v in (data.get("price_format") or {}).items()},
            auth_domain=auth.get("domain"),
            auth_client_id=auth.get("client_id"),
            keywords=tuple(str(k) for k in data.get("keywords") or ()),
        )

    @property
    def has_shop(self) -> bool:
        """False when IKEA's search returned no priced products here (e.g. a website without an online range)."""

        return bool(self.currency)

    @property
    def tested(self) -> str | None:
        """Date of the last live check of the guest features, or None."""

        return TESTED.get(self.country)

    def price_format(self, language: str | None) -> PriceFormat:
        if language in self.price_formats:
            return self.price_formats[language]
        if self.default_language in self.price_formats:
            return self.price_formats[self.default_language]
        return next(iter(self.price_formats.values()), PriceFormat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "name": self.name,
            "languages": list(self.languages),
            "language_names": dict(self.languages),
            "default_language": self.default_language,
            "currency": self.currency,
            "login": bool(self.auth_domain and self.auth_client_id),
            "shop": self.has_shop,
            "tested": self.tested,
            "url": f"https://www.ikea.com/{self.country}/{self.default_language}/",
        }


@functools.cache
def _table() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("markets.json").read_text(encoding="utf-8")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def table_date() -> str | None:
    return _table().get("generated")


@functools.cache
def all_markets() -> dict[str, Market]:
    return {cc: Market.from_dict(cc, data) for cc, data in (_table().get("markets") or {}).items()}


def other_sites() -> dict[str, dict[str, Any]]:
    """Countries with an IKEA website that is not on the shared platform (franchise or regional sites)."""

    return dict(_table().get("other_sites") or {})


def get(country: str | None) -> Market | None:
    if not country:
        return None
    return all_markets().get(country.lower())


def resolve(country: str, language: str | None = None) -> tuple[Market, str]:
    """Return (market, language) or raise UnsupportedMarket with a message that says why."""

    cc = country.lower()
    market = get(cc)
    if market is None:
        other = other_sites().get(cc)
        if other and other.get("served_by") and get(other["served_by"]):
            host = get(other["served_by"])
            assert host is not None
            raise UnsupportedMarket(
                f"IKEA {other.get('name') or cc.upper()} is served by the IKEA {host.name} website.",
                hint=f"Use `ikea config set country {host.country}`.",
            )
        if other:
            raise UnsupportedMarket(
                f"IKEA {other.get('name') or cc.upper()} runs its own website ({other.get('url')}), which ikea-cli does not support.",
                hint="Run `ikea markets` to see the countries ikea-cli works with.",
            )
        raise UnsupportedMarket(
            f"{cc!r} is not an IKEA market that ikea-cli knows.",
            hint="Run `ikea markets` to see country codes. If IKEA opened this market recently, a newer ikea-cli may support it.",
        )
    if not market.has_shop:
        raise UnsupportedMarket(
            f"The IKEA {market.name} website lists no products online at the moment, so ikea-cli cannot use it.",
            hint=f"See https://www.ikea.com/{market.country}/{market.default_language}/ for that market.",
        )
    lang = (language or market.default_language).lower()
    if lang not in market.languages:
        raise UnsupportedMarket(
            f"IKEA {market.name} is not available in {lang!r}; choose one of: {', '.join(market.languages)}.",
            hint=f"Run `ikea config set language {market.default_language}`.",
        )
    return market, lang


def search(query: str | None = None) -> list[Market]:
    """Markets whose code, name or IKEA keywords contain the query (case-insensitive)."""

    found = sorted(all_markets().values(), key=lambda m: m.name)
    if not query:
        return found
    q = query.strip().lower()
    return [m for m in found if q == m.country or q in m.name.lower() or any(q in k.lower() for k in m.keywords)]
