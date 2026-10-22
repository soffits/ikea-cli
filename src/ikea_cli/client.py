"""High-level Python API for the IKEA website.

Example::

    from ikea_cli import IkeaClient

    with IkeaClient() as ikea:
        results = ikea.search("kallax", limit=5)
        stock = ikea.stock(["20275814"], store="149")
"""

from __future__ import annotations

import contextlib
import html as html_lib
import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from . import normalize, queries
from .auth import Auth
from .config import (
    CART_BFF_CLIENT_ID,
    FAVOURITES_CLIENT_ID,
    LOCATION_CLIENT_ID,
    PURCHASES_CLIENT_ID,
    STOCK_CLIENT_ID,
    Paths,
    Settings,
    load_settings,
    read_private_json,
    write_private_json,
)
from .errors import ApiError, IkeaError, NotFound, SessionExpired, UsageError
from .transport import Transport, new_request_id

SEARCH_VERSION = "20260727"
STORES_CACHE_SECONDS = 24 * 3600
CATEGORIES_CACHE_SECONDS = 24 * 3600
MAX_PAGE = 100

SORT_OPTIONS = {
    "relevance": "RELEVANCE",
    "price-asc": "PRICE_LOW_TO_HIGH",
    "price-desc": "PRICE_HIGH_TO_LOW",
    "newest": "NEWEST",
    "rating": "RATING",
    "name": "NAME_ASCENDING",
    "popular": "MOST_POPULAR",
    "discount": "DISCOUNT_PERCENTAGE_HIGH_TO_LOW",
    "width": "WIDTH",
    "height": "HEIGHT",
    "depth": "DEPTH",
    "length": "LENGTH",
}

CART_FLAGS = {
    "fetchItemInfo": True,
    "fetchCartContext": True,
    "fetchIndicativeAvailability": False,
    "fetchPrice": True,
    "fetchDiscountCodes": True,
}

_HYDRATE_RE = re.compile(r'<script type="text/hydrate"[^>]*>(\{\s*"0"\s*:\s*\{\s*"loaderData"\s*:.*?)</script>', re.S)
_CATEGORY_LINK_RE = re.compile(
    r'<a[^>]+href="https://www\.ikea\.com/(?P<cc>[a-z]{2})/(?P<ll>[a-z]{2})/cat/(?P<slug>[a-z0-9-]+?)-(?P<id>[a-z]{2}\d{3}|\d+)/"[^>]*>(?P<inner>.*?)</a>',
    re.S,
)


class IkeaClient:
    """Talks to the IKEA web APIs for one market (country + language)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        paths: Paths | None = None,
        http: httpx.Client | None = None,
        access_token: str | None = None,
        guest: bool = False,
    ) -> None:
        self.paths = paths or Paths.default()
        self.settings = settings or load_settings(self.paths)
        self.transport = Transport(http)
        self.auth = Auth(self.settings, self.paths, self.transport, access_token=access_token)
        self.force_guest = guest

    # -- context manager

    def __enter__(self) -> IkeaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    @property
    def cc(self) -> str:
        return self.settings.country

    @property
    def lang(self) -> str:
        return self.settings.language

    # ======================================================================= authenticated requests

    def _authed(
        self, method: str, url: str, *, what: str, shopper: bool = False, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> tuple[Any, bool]:
        """Send a request with the account token (or, with ``shopper``, the guest token when not logged in).

        Returns (json, is_guest). A 401 means IKEA no longer accepts the session: account sessions get one
        forced refresh and retry for GET requests, then SessionExpired; a rejected guest token is forgotten.
        """

        if shopper:
            token, is_guest = self.auth.shopper_token(guest=self.force_guest)
        else:
            token, is_guest = self.auth.user_token(), False

        def send(tok: str) -> Any:
            return self.transport.json(method, url, headers={**(headers or {}), "authorization": f"Bearer {tok}"}, what=what, **kwargs)

        try:
            return send(token), is_guest
        except ApiError as exc:
            if exc.status != 401:
                raise
            if is_guest:
                self.auth.forget_guest()
                raise ApiError(
                    f"{what} rejected the guest session (HTTP 401).",
                    status=401,
                    hint="A new guest session will be used next time; run the command again. The old guest cart is lost.",
                ) from exc
            if method.upper() == "GET" and self.auth.can_refresh():
                fresh = self.auth.refresh(rejected_token=token)
                try:
                    return send(fresh.access_token), False
                except ApiError as exc2:
                    if exc2.status != 401:
                        raise
            raise SessionExpired("IKEA no longer accepts your session (HTTP 401).") from exc

    # ======================================================================= search

    def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        filters: Mapping[str, Any] | None = None,
        sort: str | None = None,
        limit: int = 24,
        offset: int = 0,
        store: str | None = None,
        postal_code: str | None = None,
    ) -> dict[str, Any]:
        """Search products by text, or browse a category (id such as '10653' or 'st001')."""

        if not query and not category:
            raise UsageError("Give a search text or a category.")
        if limit < 1 or limit > MAX_PAGE:
            raise UsageError(f"limit must be between 1 and {MAX_PAGE}.")
        if offset < 0:
            raise UsageError("offset must not be negative.")
        sort_id = None
        if sort:
            sort_id = SORT_OPTIONS.get(sort.lower()) or sort.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", sort_id):
                raise UsageError(
                    f"Unknown sort {sort!r}. Use one of {', '.join(SORT_OPTIONS)}, or an id listed by `ikea search TEXT --filters`."
                )
        filter_config: dict[str, Any] = {"subcategories-style": "tree-navigation", "max-num-filters": 30}
        for key, value in (filters or {}).items():
            param = key if key.startswith("f-") else f"f-{key}"
            if value is True or value == "true":
                filter_config[param] = True
            elif isinstance(value, (list, tuple)):
                filter_config[param] = [str(v) for v in value]
            else:
                filter_config[param] = [v for v in str(value).split(",") if v]
        if category and query:
            # Text search inside a category: IKEA's subcategory filter keeps totals and paging right.
            filter_config["f-subcategories"] = [category]
        component: dict[str, Any] = {
            "component": "PRIMARY_AREA",
            "columns": 4,
            "types": {"main": "PRODUCT", "breakouts": []},
            "filterConfig": filter_config,
            "window": {"size": limit, "offset": offset},
            "forceFilterCalculation": True,
            "allVariants": False,
        }
        if sort_id:
            component["sort"] = sort_id
        body: dict[str, Any] = {
            "searchParameters": {"input": query or category, "type": "QUERY" if query else "CATEGORY"},
            "allowAutocorrect": True,
            "isUserLoggedIn": False,
            "isB2B": False,
            "components": [component],
        }
        store = store or self.settings.store
        postal_code = postal_code or self.settings.postal_code
        if store:
            body["store"] = store
        if postal_code:
            body["zip"] = postal_code
        data = self.transport.json(
            "POST",
            f"https://sik.search.blue.cdtapps.com/{self.cc}/{self.lang}/search",
            params={"c": "sr", "v": SEARCH_VERSION},
            content=json.dumps(body),
            headers={"content-type": "text/plain;charset=UTF-8", "accept": "*/*", "accept-language": self.lang},
            what="IKEA search",
        )
        return normalize.search_result(data, query=query, category=category)

    def suggest(self, prefix: str) -> list[str]:
        params: dict[str, Any] = {"q": prefix, "c": "sb", "v": SEARCH_VERSION}
        if self.settings.store:
            params["store"] = self.settings.store
        data = self.transport.json(
            "GET",
            f"https://sik.search.blue.cdtapps.com/{self.cc}/{self.lang}/search-box",
            params=params,
            headers={"accept": "*/*"},
            what="IKEA search",
        )
        return normalize.suggestions(data or {})

    def categories(self, query: str | None = None) -> list[dict[str, Any]]:
        """List product categories from the market's category index page."""

        cache_file = self.paths.cache_dir / f"categories-{self.cc}-{self.lang}.json"
        cats = _read_cache(cache_file, "categories", CATEGORIES_CACHE_SECONDS)
        if cats is None:
            resp = self.transport.request(
                "GET", f"https://www.ikea.com/{self.cc}/{self.lang}/cat/products-products/", headers={"accept": "text/html"}, what="IKEA"
            )
            if resp.status_code != 200:
                raise ApiError(f"IKEA category page returned HTTP {resp.status_code}.", status=resp.status_code)
            cats = parse_categories(resp.text)
            write_private_json(cache_file, {"at": time.time(), "categories": cats})
        if query:
            q = query.lower()
            cats = [c for c in cats if q in str(c.get("name") or "").lower() or q in str(c["id"]).lower()]
        return cats

    # ======================================================================= products

    def product_summary(self, ref: str) -> dict[str, Any]:
        item_no, kind = normalize.parse_item_ref(ref)
        candidates = ["s" + item_no, item_no] if kind == "SPR" else [item_no, "s" + item_no]
        for cand in candidates:
            resp = self.transport.request(
                "GET", f"https://www.ikea.com/{self.cc}/{self.lang}/products/{item_no[-3:]}/{cand}.json", what="IKEA products"
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    continue
                out = normalize.product_summary(data)
                out["item_type"] = "SPR" if cand.startswith("s") else "ART"
                out["item_no"] = item_no
                return out
            if resp.status_code not in (403, 404):
                raise ApiError(f"IKEA products returned HTTP {resp.status_code}.", status=resp.status_code)
        raise NotFound(f"No product {normalize.format_item_no(item_no)} in IKEA {self.cc.upper()}.")

    def product(self, ref: str) -> dict[str, Any]:
        """Full product details: price, rating, dimensions, packages, materials, variants, images."""

        summary = self.product_summary(ref)
        url = summary.get("url") or f"https://www.ikea.com/{self.cc}/{self.lang}/p/-{summary['item_no']}/"
        resp = self.transport.request("GET", url, headers={"accept": "text/html", "accept-language": self.lang}, what="IKEA")
        if resp.status_code != 200:
            summary["details_available"] = False
            return summary
        page = extract_page_props(resp.text)
        if not page:
            summary["details_available"] = False
            return summary
        detail = normalize.product_detail(page, summary)
        detail["item_no"] = summary["item_no"]
        detail["item_type"] = summary.get("item_type") or detail.get("item_type")
        detail["url"] = summary.get("url") or url
        detail["details_available"] = True
        return detail

    # ======================================================================= stores and stock

    def stores(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        cache_file = self.paths.cache_dir / f"stores-{self.cc}-{self.lang}.json"
        cached = None if refresh else _read_cache(cache_file, "stores", STORES_CACHE_SECONDS)
        if cached is not None:
            return cached
        data = self.transport.json(
            "GET", f"https://www.ikea.com/{self.cc}/{self.lang}/meta-data/informera/stores-detailed.json", what="IKEA stores"
        )
        stores = [normalize.store(s) for s in data or [] if isinstance(s, dict) and not s.get("hideStore")]
        stores.sort(key=lambda s: str(s["name"]))
        write_private_json(cache_file, {"at": time.time(), "stores": stores})
        return stores

    def find_store(self, ref: str) -> dict[str, Any]:
        stores = self.stores()
        for s in stores:
            if str(s["id"]) == str(ref):
                return s
        needle = ref.lower()
        matches = [s for s in stores if needle in str(s["name"]).lower() or needle in str(s.get("city") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NotFound(f"No IKEA store matches {ref!r}.", hint="Run `ikea stores` to see store ids.")
        names = ", ".join(f"{s['id']} ({s['name']})" for s in matches)
        raise UsageError(f"{ref!r} matches several stores: {names}. Use the store id.")

    def stock(self, refs: Iterable[str], *, store: str | None = None) -> list[dict[str, Any]]:
        """Store stock and restock dates. With `store`, only that store is returned."""

        item_nos = list(dict.fromkeys(normalize.parse_item_ref(r)[0] for r in refs))
        if not item_nos:
            raise UsageError("Give at least one item number.")
        if len(item_nos) > 50:
            raise UsageError("Check at most 50 items at a time.")
        data = self.transport.json(
            "GET",
            f"https://api.salesitem.ingka.com/availabilities/ru/{self.cc}",
            params={"itemNos": ",".join(item_nos), "expand": "StoresList,Restocks,SalesLocations"},
            headers={"x-client-id": STOCK_CLIENT_ID, "accept": "application/json;version=2", "accept-language": self.lang},
            what="IKEA stock",
        )
        stores_by_id = {str(s["id"]): s for s in self.stores()}
        result = normalize.stock(data or {}, stores_by_id, self.lang)
        found = {r["item_no"] for r in result}
        for missing in item_nos:
            if missing not in found:
                result.append({"item_no": missing, "item_type": None, "home_delivery": None, "stores": [], "error": "not sold here"})
        store = store or None
        if store:
            for r in result:
                r["stores"] = [s for s in r["stores"] if s["store_id"] == str(store)]
        return result

    # ======================================================================= delivery

    def postal_lookup(self, postal_code: str) -> dict[str, Any]:
        data = self.transport.json(
            "POST",
            "https://api.ingka.ikea.com/address/location",
            json_body={"postCode": postal_code, "country": self.cc, "language": self.lang},
            headers={"x-client-id": LOCATION_CLIENT_ID, "content-type": "application/json"},
            what="IKEA address lookup",
        )
        matches = (data or {}).get("matches") or []
        if not matches:
            raise NotFound(f"IKEA does not recognise the postal code {postal_code!r}.")
        m = matches[0]
        return {"postal_code": (data or {}).get("postCode") or postal_code, "province": m.get("stateCode"), "city": m.get("city")}

    def delivery(
        self,
        items: Iterable[tuple[str, int]] | None = None,
        *,
        postal_code: str | None = None,
        store: str | None = None,
        from_cart: bool = False,
    ) -> dict[str, Any]:
        """Home delivery and click & collect options for a postal code (no checkout involved)."""

        postal_code = postal_code or self.settings.postal_code
        if not postal_code:
            raise UsageError("A postal code is needed.", hint="Pass --postal-code or run `ikea config set postal_code 'A1A 1A1'`.")
        store = store or self.settings.store
        lines: list[dict[str, Any]] = []
        if from_cart:
            current = self.cart()
            for it in current["items"]:
                lines.append(
                    {
                        "itemNo": it["item_no"],
                        "quantity": it["quantity"] or 1,
                        "type": it.get("item_type") or "ART",
                        "childItems": [{"itemNo": c["item_no"], "quantity": c["quantity"]} for c in it.get("children") or []],
                    }
                )
        else:
            for ref, qty in items or []:
                item_no, kind = normalize.parse_item_ref(ref)
                lines.append({"itemNo": item_no, "quantity": qty, "type": kind or "ART", "childItems": []})
        if not lines:
            raise UsageError("Nothing to check: give item numbers or use the cart.")
        loc = self.postal_lookup(postal_code)
        data, _ = self._authed(
            "POST",
            "https://web-api.ikea.com/cart-bff-cf/api/v2/fulfilment",
            shopper=True,
            json_body={
                "address": {
                    "postalCode": loc["postal_code"],
                    "stateCode": loc["province"],
                    "storeId": store or "",
                    "country": self.cc,
                    "language": self.lang,
                },
                "items": lines,
                "planners": [],
                "consumerName": "Web",
                "flags": {"restock": True, "fetchFulfilmentPrices": True, "fetchPUPsAvailability": False},
            },
            headers={
                "x-client-id": CART_BFF_CLIENT_ID,
                "x-request-id": new_request_id(),
                "content-type": "application/json",
                "accept-language": self.lang,
            },
            what="IKEA delivery",
        )
        out = normalize.fulfilment(data or {})
        out["postal_code"] = loc["postal_code"]
        out["province"] = loc["province"]
        out["city"] = loc["city"]
        out["store_id"] = store
        return out

    # ======================================================================= cart

    def _cart_base(self) -> str:
        return f"https://prod.cart.caas.selling.ingka.com/api/v1/{self.cc}"

    def _cart_params(self) -> dict[str, str]:
        params = {k: "true" if v else "false" for k, v in CART_FLAGS.items()}
        params.update({"shoppingProfile": "ONLINE", "group": "DEFAULT"})
        if self.settings.postal_code:
            params["postCode"] = self.settings.postal_code
        return params

    def _cart_body(self, **extra: Any) -> dict[str, Any]:
        populate: dict[str, Any] = dict(CART_FLAGS)
        if self.settings.postal_code:
            populate["postCode"] = self.settings.postal_code
        return {"shoppingProfile": "ONLINE", "group": "DEFAULT", "populateCart": populate, **extra}

    def _cart_result(self, data: Any, is_guest: bool) -> dict[str, Any]:
        out = normalize.cart(data or {})
        out["guest"] = is_guest
        return out

    def _cart(self, method: str, path: str = "", **kwargs: Any) -> tuple[Any, bool]:
        headers = {"x-consumer-name": "Web Cart", "accept-language": self.lang, "accept": "*/*", "content-type": "application/json"}
        return self._authed(method, self._cart_base() + path, shopper=True, what="IKEA cart", headers=headers, **kwargs)

    def cart(self) -> dict[str, Any]:
        data, is_guest = self._cart("GET", params=self._cart_params())
        return self._cart_result(data, is_guest)

    def cart_add(self, items: Iterable[tuple[str, int]]) -> dict[str, Any]:
        lines = _cart_lines(items)
        data, is_guest = self._cart("POST", "/items", json_body=self._cart_body(items=lines))
        return self._cart_result(data, is_guest)

    def _require_in_cart(self, item_nos: Iterable[str], hint: str) -> None:
        current = {i["item_no"] for i in self.cart()["items"]}
        missing = [n for n in item_nos if n not in current]
        if missing:
            raise NotFound(f"Not in the cart: {', '.join(normalize.format_item_no(m) for m in missing)}.", hint=hint)

    def cart_set(self, items: Iterable[tuple[str, int]]) -> dict[str, Any]:
        """Set absolute quantities. A quantity of 0 removes the line. Every item must already be in the cart."""

        lines = _cart_lines(items, allow_zero=True)
        zero = [line["itemNo"] for line in lines if line["quantity"] == 0]
        keep = [line for line in lines if line["quantity"] > 0]
        self._require_in_cart([line["itemNo"] for line in lines], "Use `ikea cart add` to add new items.")
        result = None
        if keep:
            data, is_guest = self._cart("PUT", "/items", json_body=self._cart_body(items=keep))
            result = self._cart_result(data, is_guest)
        if zero:
            result = self._delete_items(zero)
        assert result is not None
        return result

    def cart_remove(self, refs: Iterable[str]) -> dict[str, Any]:
        """Remove lines. Fails without changing anything if an item is not in the cart."""

        item_nos = list(dict.fromkeys(normalize.parse_item_ref(r)[0] for r in refs))
        if not item_nos:
            raise UsageError("Give at least one item number to remove.")
        self._require_in_cart(item_nos, "Check the item numbers with `ikea cart`.")
        return self._delete_items(item_nos)

    def _delete_items(self, item_nos: list[str]) -> dict[str, Any]:
        params = self._cart_params()
        params["itemNumbers"] = ",".join(item_nos)
        data, is_guest = self._cart("DELETE", "/items", params=params)
        return self._cart_result(data, is_guest)

    def cart_clear(self) -> dict[str, Any]:
        # A cart that was never created answers 404; it is already empty.
        with contextlib.suppress(NotFound):
            self._cart("DELETE", "/groups/DEFAULT")
        return self.cart()

    def cart_add_coupon(self, code: str) -> dict[str, Any]:
        code = code.strip()
        if not code:
            raise UsageError("Give a discount code.")
        data, is_guest = self._cart("POST", "/discount-codes", json_body=self._cart_body(discountCodes=[code]))
        return self._cart_result(data, is_guest)

    def cart_remove_coupon(self, code: str) -> dict[str, Any]:
        params = self._cart_params()
        params["discountCodes"] = code.strip()
        data, is_guest = self._cart("DELETE", "/discount-codes", params=params)
        return self._cart_result(data, is_guest)

    # ======================================================================= favourites

    def _favs(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        base_vars = {"languageCode": self.lang, "storeId": self.settings.store, "zip": None}
        data, _ = self._authed(
            "POST",
            "https://favs.oneweb.ingka.com/graphql",
            shopper=True,
            json_body={"query": query, "variables": {**base_vars, **variables}},
            headers={"x-client-id": FAVOURITES_CLIENT_ID, "content-type": "application/json"},
            what="IKEA favourites",
        )
        return _graphql_data(data, "IKEA favourites")

    def lists(self) -> list[dict[str, Any]]:
        data = self._favs(queries.FAV_LISTS, {})
        return [normalize.fav_list(lst) for lst in data.get("lists") or []]

    def resolve_list(self, ref: str | None) -> str | None:
        """Turn a list id or name into a list id. None means the selected (default) list."""

        if not ref:
            return None
        lists = self.lists()
        for lst in lists:
            if lst["list_id"] == ref:
                return ref
        matches = [lst for lst in lists if lst["name"].lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0]["list_id"]
        if len(matches) > 1:
            raise UsageError(f"Several lists are called {ref!r}; use the list id.")
        raise NotFound(f"No favourites list called {ref!r}.", hint="Run `ikea lists` to see your lists.")

    def get_list(self, ref: str | None = None) -> dict[str, Any]:
        list_id = self.resolve_list(ref)
        data = self._favs(queries.FAV_LIST, {"listId": list_id})
        if not data.get("list"):
            raise NotFound("That favourites list does not exist.")
        return normalize.fav_list(data["list"])

    def list_create(self, name: str) -> dict[str, Any]:
        data = self._favs(queries.FAV_CREATE, {"name": name})
        return normalize.fav_list(data.get("list") or {})

    def list_rename(self, ref: str, name: str) -> dict[str, Any]:
        list_id = self.resolve_list(ref)
        data = self._favs(queries.FAV_RENAME, {"listId": list_id, "name": name})
        return normalize.fav_list(data.get("list") or {})

    def list_delete(self, ref: str) -> list[dict[str, Any]]:
        list_id = self.resolve_list(ref)
        data = self._favs(queries.FAV_DELETE, {"listId": list_id})
        return [normalize.fav_list(lst) for lst in data.get("lists") or []]

    def list_select(self, ref: str) -> list[dict[str, Any]]:
        list_id = self.resolve_list(ref)
        data = self._favs(queries.FAV_SELECT, {"listId": list_id})
        return [normalize.fav_list(lst) for lst in data.get("lists") or []]

    def list_add(self, items: Iterable[tuple[str, int]], *, list_ref: str | None = None) -> dict[str, Any]:
        lines = _cart_lines(items)
        data = self._favs(queries.FAV_ADD, {"items": lines, "listId": self.resolve_list(list_ref), "ignoreUnavailable": False})
        return normalize.fav_list(data.get("list") or {})

    def list_set(self, items: Iterable[tuple[str, int]], *, list_ref: str | None = None) -> dict[str, Any]:
        lines = _cart_lines(items)
        data = self._favs(queries.FAV_UPDATE, {"items": lines, "listId": self.resolve_list(list_ref)})
        return normalize.fav_list(data.get("list") or {})

    def list_remove(self, refs: Iterable[str], *, list_ref: str | None = None) -> dict[str, Any]:
        item_nos = list(dict.fromkeys(normalize.parse_item_ref(r)[0] for r in refs))
        data = self._favs(queries.FAV_REMOVE, {"itemNos": item_nos, "listId": self.resolve_list(list_ref)})
        return normalize.fav_list(data.get("list") or {})

    # ======================================================================= orders

    def _purchases(self, query: str, variables: dict[str, Any], *, allow_not_found: bool = False) -> dict[str, Any]:
        data, _ = self._authed(
            "POST",
            "https://cssom-prod.ingka.com/purchase-history/graphql",
            json_body={"query": query, "variables": variables},
            headers={
                "x-client-id": PURCHASES_CLIENT_ID,
                "x-cssom-build": "standalone",
                "accept": "application/graphql-response+json,application/json;q=0.9",
                "accept-language": self.settings.locale.lower(),
                "content-type": "application/json",
            },
            what="IKEA purchases",
        )
        if allow_not_found and isinstance(data, dict) and data.get("errors"):
            codes = [_dig_code(e) for e in data["errors"]]
            if 404 in codes:
                return {}
        return _graphql_data(data, "IKEA purchases")

    def orders(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if limit < 1 or offset < 0 or offset + limit > 500:
            raise UsageError("Use a limit of at least 1 and keep offset + limit at 500 or less.")
        # The service's `skip` counts pages of `take` items, so fetch from the start and slice.
        data = self._purchases(queries.PURCHASE_HISTORY, {"skip": 0, "take": offset + limit})
        history = data.get("historyData") or {}
        purchases = [normalize.purchase_summary(p) for p in (history.get("historicalPurchases") or [])[offset:]]
        return {"total": history.get("totalPurchases"), "offset": offset, "count": len(purchases), "purchases": purchases}

    def order(self, number: str) -> dict[str, Any]:
        number = number.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{4,40}", number):
            raise UsageError(f"{number!r} does not look like an order or receipt number.")
        data = self._purchases(queries.ORDER_DETAIL, {"orderNumber": number, "skip": 0, "take": 200}, allow_not_found=True)
        if data.get("order"):
            return normalize.purchase_detail(data["order"], "order")
        data = self._purchases(queries.RECEIPT_DETAIL, {"receiptNumber": number, "skip": 0, "take": 200}, allow_not_found=True)
        if data.get("receipt"):
            return normalize.purchase_detail(data["receipt"], "receipt")
        raise NotFound(f"No purchase {number} on this account.", hint="Run `ikea orders` to list your purchases.")

    def order_status(self, number: str) -> dict[str, Any]:
        data = self._purchases(queries.ORDER_STATUS, {"orderNumber": number.strip()}, allow_not_found=True)
        order = data.get("order")
        if not order:
            raise NotFound(f"No order {number} on this account.")
        return {
            "id": order.get("id"),
            "status": order.get("status"),
            "deliveries": normalize.delivery_methods(order.get("deliveryMethods")),
        }

    # ======================================================================= loyalty

    def _account_get(self, url: str, *, what: str, headers: dict[str, str] | None = None) -> Any:
        return self._authed("GET", url, headers={"accept": "*/*", "accept-language": self.lang, **(headers or {})}, what=what)[0]

    def _account_post(self, url: str, body: Any, *, what: str) -> Any:
        return self._authed(
            "POST",
            url,
            json_body=body,
            headers={
                "content-type": "application/json",
                "accept-language": self.lang,
                "x-correlation-id": new_request_id(),
            },
            what=what,
        )[0]

    def loyalty(self) -> dict[str, Any]:
        """IKEA Family membership and rewards points."""

        customer = self._account_get(f"https://api.wlo.ingka.com/loyalty/v1/{self.cc}/customer-data", what="IKEA Family")
        cust = ((customer or {}).get("customers") or [{}])[0]
        memberships = [
            {
                "program": m.get("programCode"),
                "status": m.get("status"),
                "member_since": (m.get("created") or "")[:10] or None,
            }
            for m in cust.get("loyaltyMemberships") or []
        ]
        card = cust.get("activeLoyaltyMembershipCard") or {}
        balance = self._account_get("https://api.wlo.ingka.com/loyalty/v1/token-balance", what="IKEA rewards")
        stores = {str(s["id"]): s["name"] for s in self._safe(self.stores, [])}
        preferred = cust.get("preferredBUCode")
        return {
            "name": cust.get("firstName"),
            "member": any(m["status"] == "ACTIVE" for m in memberships),
            "memberships": memberships,
            "card_number": normalize.mask(card.get("cardNumber")),
            "card_status": card.get("cardStatus"),
            "points": (balance or {}).get("tokenBalance"),
            "preferred_store": {"id": preferred, "name": stores.get(str(preferred))} if preferred else None,
        }

    def rewards(self) -> dict[str, Any]:
        """Rewards you can redeem with points, and rewards you have already claimed."""

        base = f"https://www.ikea.com/{self.cc}/{self.lang}/lower-funnel-fragments/api/v1"
        balance = self._account_post(f"{base}/rewards-service/balance/", {"address": {"country": self.cc}}, what="IKEA rewards")
        offers = self._account_post(
            f"{base}/rewards-service/offers/", {"address": {"country": self.cc, "language": self.lang}}, what="IKEA rewards"
        )
        claimed = self._account_get(
            "https://api.rew.ingka.com/rew/v3/rewards",
            what="IKEA rewards",
            headers={"api-version": "2020-11-11", "content-type": "application/json"},
        )
        out_offers = []
        for o in offers or []:
            content = o.get("content") or {}
            out_offers.append(
                {
                    "code": o.get("offerCode"),
                    "name": content.get("name"),
                    "points": o.get("amount"),
                    "category": o.get("category"),
                    "status": o.get("status"),
                    "description": _strip_html(content.get("description")),
                }
            )
        out_offers.sort(key=lambda x: x["points"] or 0)
        # The claimed-rewards shape could not be checked (it needs an account with claimed rewards); read only
        # plain fields and leave the rest empty rather than guessing.
        claimed_items = []
        for r in (claimed or {}).get("items") or []:
            if not isinstance(r, dict):
                continue
            content = r.get("content") if isinstance(r.get("content"), dict) else {}
            name = r.get("name") or r.get("title") or content.get("name")
            claimed_items.append(
                {
                    "name": name if isinstance(name, str) else None,
                    "status": r.get("status"),
                    "valid_to": r.get("validTo") or r.get("expiryDate"),
                }
            )
        return {"points": (balance or {}).get("keysBalance"), "offers": out_offers, "claimed": claimed_items}

    def profile(self) -> dict[str, Any]:
        data, _ = self._authed(
            "POST",
            "https://api.wlo.ingka.com/cms/api/profile/graphql",
            json_body={
                "query": f"{{getCustomerProfile(retailUnit: {self.cc}) {{customerIdentity {{ firstName lastName email "
                "loyaltyProgramCode preferredStore zipCode emailVerified }}}"
            },
            headers={"content-type": "application/json"},
            what="IKEA profile",
        )
        ident = (_graphql_data(data, "IKEA profile").get("getCustomerProfile") or {}).get("customerIdentity") or {}
        return {
            "first_name": ident.get("firstName"),
            "last_name": ident.get("lastName"),
            "email": ident.get("email"),
            "loyalty_program": ident.get("loyaltyProgramCode"),
            "preferred_store": ident.get("preferredStore"),
            "postal_code": ident.get("zipCode"),
        }

    @staticmethod
    def _safe(fn: Any, default: Any) -> Any:
        try:
            return fn()
        except IkeaError:
            return default


# --------------------------------------------------------------------------- helpers


def _read_cache(path: Any, key: str, max_age: float) -> list[dict[str, Any]] | None:
    """Return a cached list of dicts if the cache file is fresh and well-formed, else None."""

    cached = read_private_json(path)
    if not isinstance(cached, dict):
        return None
    try:
        age = time.time() - float(cached.get("at", 0))
    except (TypeError, ValueError):
        return None
    items = cached.get(key)
    if age >= max_age or not isinstance(items, list) or not all(isinstance(i, dict) and "id" in i for i in items):
        return None
    return items


def _cart_lines(items: Iterable[tuple[str, int]], *, allow_zero: bool = False) -> list[dict[str, Any]]:
    merged: dict[str, int] = {}
    for ref, qty in items:
        item_no, _ = normalize.parse_item_ref(ref)
        try:
            q = int(qty)
        except (TypeError, ValueError) as exc:
            raise UsageError(f"Quantity for {ref} must be a whole number.") from exc
        if q < 0 or (q == 0 and not allow_zero) or q > 999:
            raise UsageError(f"Quantity for {ref} must be between {0 if allow_zero else 1} and 999.")
        merged[item_no] = merged.get(item_no, 0) + q if not allow_zero else q
    if not merged:
        raise UsageError("Give at least one item.")
    return [{"itemNo": k, "quantity": v} for k, v in merged.items()]


def _dig_code(err: Any) -> Any:
    if isinstance(err, dict):
        ext = err.get("extensions") or {}
        return ext.get("errorCode") or ext.get("code")
    return None


def _graphql_data(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiError(f"{what} returned an unexpected response.")
    errors = data.get("errors") or []
    if errors and not data.get("data"):
        first = errors[0] if isinstance(errors[0], dict) else {}
        code = _dig_code(first)
        message = first.get("message") or "unknown error"
        if code in ("LIST_NOT_FOUND", 404):
            raise NotFound(f"{what}: {message}", details={"codes": [code]})
        if code in ("UNAUTHENTICATED", 401):
            from .errors import SessionExpired

            raise SessionExpired()
        raise ApiError(f"{what}: {message}", details={"codes": [c for c in (_dig_code(e) for e in errors) if c]})
    return data.get("data") or {}


def extract_page_props(page_html: str) -> dict[str, Any] | None:
    """Find the product page's main data block among the page's embedded JSON scripts."""

    for m in _HYDRATE_RE.finditer(page_html):
        if '"pageProps"' not in m.group(1)[:200]:
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        props = data.get("0", {}).get("loaderData", {}).get("pageProps")
        if isinstance(props, dict) and isinstance(props.get("product"), dict):
            return props
    return None


def parse_categories(page_html: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for m in _CATEGORY_LINK_RE.finditer(page_html):
        cid = m.group("id")
        text = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group("inner"))).split())
        name = text or m.group("slug").replace("-", " ").capitalize()
        if cid not in seen or (not seen[cid]["name"] and name):
            seen[cid] = {
                "id": cid,
                "name": name,
                "url": f"https://www.ikea.com/{m.group('cc')}/{m.group('ll')}/cat/{m.group('slug')}-{cid}/",
            }
    return list(seen.values())


def _strip_html(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", value)).split()) or None
