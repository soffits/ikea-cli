"""Turn raw IKEA API responses into small, stable dictionaries.

These shapes are what `--json` prints and what library users get back. Keep them flat,
keep key names stable, and add a field rather than renaming one.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import UsageError

_ITEM_RE = re.compile(r"^(?P<prefix>[sS])?(?P<digits>\d{8})$")
_URL_ITEM_RE = re.compile(r"-(?P<prefix>s)?(?P<digits>\d{8})/?(?:[?#].*)?$")


def parse_item_ref(ref: str) -> tuple[str, str | None]:
    """Accept '30449908', '304.499.08', 's09563608' or a product URL. Return (8 digits, 'SPR'|'ART'|None)."""

    raw = str(ref).strip()
    if raw.startswith(("http://", "https://")) or "/p/" in raw:
        m = _URL_ITEM_RE.search(raw.split("?")[0].split("#")[0].rstrip("/") + "/")
        if not m:
            raise UsageError(f"Could not find an item number in {ref!r}.")
        return m.group("digits"), ("SPR" if m.group("prefix") else "ART")
    compact = raw.replace(".", "").replace(" ", "").replace("-", "")
    m = _ITEM_RE.match(compact)
    if not m:
        raise UsageError(f"{ref!r} is not an IKEA item number (8 digits, e.g. 304.499.08).")
    return m.group("digits"), ("SPR" if m.group("prefix") else None)


def format_item_no(item_no: str) -> str:
    d = re.sub(r"\D", "", item_no)
    return f"{d[0:3]}.{d[3:6]}.{d[6:8]}" if len(d) == 8 else item_no


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _price_pair(obj: Any) -> float | None:
    """Pick inclTax when present, else exclTax (Canadian prices are shown before tax)."""

    if not isinstance(obj, dict):
        return _num(obj)
    incl = _num(obj.get("inclTax"))
    return incl if incl is not None else _num(obj.get("exclTax"))


def _dig(obj: Any, *path: Any) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


# --------------------------------------------------------------------------- search


def search_product(p: dict[str, Any]) -> dict[str, Any]:
    price = p.get("salesPrice") or {}
    category = None
    path = p.get("categoryPath")
    if isinstance(path, list) and path:
        last = path[-1]
        if isinstance(last, dict):
            category = {"id": last.get("key"), "name": last.get("name")}
    return {
        "item_no": p.get("itemNo") or p.get("id"),
        "item_type": p.get("itemType"),
        "name": p.get("name"),
        "type_name": p.get("typeName"),
        "description": p.get("validDesignText") or None,
        "measure": p.get("itemMeasureReferenceText") or None,
        "price": _num(price.get("numeral")),
        "currency": price.get("currencyCode"),
        "rating": _num(p.get("ratingValue")),
        "rating_count": p.get("ratingCount"),
        "online_sellable": p.get("onlineSellable"),
        "variants": _dig(p, "gprDescription", "numberOfVariants") or 0,
        "category": category,
        "url": p.get("pipUrl"),
        "image": p.get("mainImageUrl"),
    }


def search_filters(raw_filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in raw_filters or []:
        entry: dict[str, Any] = {"parameter": f.get("parameter"), "name": f.get("name"), "type": f.get("type")}
        if f.get("type") == "BOOLEAN":
            entry["values"] = [{"id": "true", "name": "yes", "selected": bool(f.get("selected"))}]
        elif f.get("type") == "TYPED_CLASS_FILTER":
            values = []
            for t in f.get("types") or []:
                for v in t.get("values") or []:
                    values.append(
                        {
                            "id": v.get("id"),
                            "name": f"{t.get('name')}: {v.get('name')}",
                            "count": v.get("count"),
                            "selected": v.get("selected"),
                        }
                    )
            entry["values"] = values
        elif f.get("type") == "CATEGORY_TREE":
            cats = f.get("categories") or f.get("values") or []
            entry["values"] = [
                {"id": c.get("key") or c.get("id"), "name": c.get("name"), "count": c.get("count"), "selected": c.get("selected")}
                for c in cats
                if isinstance(c, dict)
            ]
        else:
            entry["values"] = [
                {"id": v.get("id"), "name": v.get("name"), "count": v.get("count"), "selected": v.get("selected")}
                for v in f.get("values") or []
            ]
        out.append(entry)
    return out


def search_result(data: dict[str, Any], *, query: str | None, category: str | None) -> dict[str, Any]:
    primary = next((r for r in data.get("results") or [] if r.get("component") == "PRIMARY_AREA"), None) or {}
    summary = next((r for r in data.get("results") or [] if r.get("component") == "SEARCH_SUMMARY"), None) or {}
    meta = primary.get("metadata") or {}
    items = [search_product(i["product"]) for i in primary.get("items") or [] if i.get("type") == "PRODUCT" and i.get("product")]
    sort_values = _dig(primary, "sortOrders", "values") or []
    selected_sort = next((s.get("id") for s in sort_values if s.get("selected")), None)
    out: dict[str, Any] = {
        "query": query,
        "category": category,
        "total": meta.get("max", 0),
        "offset": meta.get("start", 0),
        "count": len(items),
        "sort": selected_sort,
        "items": items,
        "filters": search_filters(primary.get("filters") or []),
        "sort_options": [{"id": s.get("id"), "name": s.get("name")} for s in sort_values],
    }
    if summary.get("didYouMeanPhrases"):
        out["did_you_mean"] = summary["didYouMeanPhrases"]
    if summary.get("originalQuery") and query and summary.get("queryWasExpanded"):
        out["expanded_query"] = True
    return out


def suggestions(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    box = data.get("searchBox") or {}
    for group in box.values():
        if not isinstance(group, list):
            continue
        for entry in group:
            text = _dig(entry, "completion", "text")
            if isinstance(text, str) and text not in out:
                out.append(text)
    return out


# --------------------------------------------------------------------------- product


def product_summary(data: dict[str, Any]) -> dict[str, Any]:
    rating = _dig(data, "experimental", "rating") or {}
    return {
        "item_no": data.get("id"),
        "name": data.get("name"),
        "type_name": data.get("typeName"),
        "description": data.get("validDesignText") or None,
        "price": _num(data.get("priceNumeral")),
        "price_excl_tax": _num(data.get("priceExclTaxNumeral")),
        "currency": data.get("currencyCode"),
        "rating": _num(rating.get("value")),
        "rating_count": rating.get("count"),
        "is_new": _dig(data, "experimental", "isNewProduct"),
        "url": data.get("pipUrl"),
        "image": _dig(data, "mainImage", "url"),
    }


def product_detail(page: dict[str, Any], summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build product details from the product page's embedded data (`pageProps`)."""

    product = page.get("product") or {}
    info = page.get("productInformationSectionProps") or {}
    details = info.get("productDetailsProps") or {}
    measurements = info.get("measurementsProps") or {}
    accordion = details.get("accordionObject") or {}
    out: dict[str, Any] = dict(summary or {})
    out.update(
        {
            "item_no": product.get("itemNo") or out.get("item_no"),
            "formatted_item_no": product.get("visibleItemNo"),
            "item_type": product.get("type"),
            "name": product.get("name") or out.get("name"),
            "description": product.get("description") or out.get("description"),
            "measure": product.get("itemMeasureReferenceText") or None,
            "price": _num(product.get("price")) if product.get("price") is not None else out.get("price"),
            "currency": product.get("currencyCode") or out.get("currency"),
            "online_sellable": product.get("isServerOnlineSellable"),
            "rating": _num(_dig(product, "ratingProps", "rating")) or out.get("rating"),
            "rating_count": product.get("reviewCount", out.get("rating_count")),
        }
    )
    out["summary"] = [p for p in (_dig(details, "productDescriptionProps", "paragraphs") or []) if isinstance(p, str)]
    out["designer"] = _dig(details, "productDescriptionProps", "designerName")
    out["dimensions"] = [
        {"name": m.get("name"), "value": m.get("measure")} for m in measurements.get("measurements") or [] if isinstance(m, dict)
    ]
    packaging = _dig(measurements, "packaging", "contentProps") or product.get("packaging") or {}
    packages = []
    for pkg in packaging.get("packages") or []:
        dims = {}
        for group in pkg.get("measurementGroups") or []:
            for m in group.get("measurements") or []:
                if m.get("type"):
                    dims[m["type"]] = m.get("text")
        packages.append(
            {
                "item_no": pkg.get("itemNo"),
                "name": " ".join(x for x in (pkg.get("name"), pkg.get("typeName")) if x),
                "quantity": _dig(pkg, "quantity", "value"),
                **dims,
            }
        )
    out["number_of_packages"] = packaging.get("numberOfPackages")
    out["packages"] = packages
    materials = []
    for block in _dig(accordion, "materialsAndCare", "contentProps", "materials") or []:
        for m in block.get("materials") or []:
            materials.append(" ".join(x for x in (m.get("part"), m.get("material")) if x))
    out["materials"] = materials
    care = []
    for block in _dig(accordion, "materialsAndCare", "contentProps", "careInstructions") or []:
        care.extend(t for t in block.get("texts") or [] if isinstance(t, str))
    out["care"] = care
    variations = []
    for v in _dig(page, "productSpecificationSectionProps", "variations") or []:
        variations.append(
            {
                "title": v.get("title"),
                "selected": v.get("selectedOption"),
                "options": [
                    {"name": o.get("title"), "item_no": o.get("linkId"), "selected": bool(o.get("isSelected")), "url": o.get("url")}
                    for o in v.get("options") or []
                ],
            }
        )
    out["variants"] = variations
    images = []
    for media in product.get("mediaList") or []:
        if media.get("type") == "image":
            url = _dig(media, "content", "url")
            if url:
                images.append(url)
    out["images"] = images
    out["secondary_ratings"] = {
        r.get("id"): r.get("value") for r in product.get("secondaryRatings") or [] if isinstance(r, dict) and r.get("id")
    }
    return out


# --------------------------------------------------------------------------- stores and stock


def store(raw: dict[str, Any]) -> dict[str, Any]:
    address = raw.get("address") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("displayName") or raw.get("name"),
        "kind": _dig(raw, "buClassification", "name"),
        "city": address.get("city"),
        "province": _province(address.get("stateProvinceCode")),
        "address": address.get("displayAddress") or address.get("street"),
        "postal_code": address.get("zipCode"),
        "timezone": address.get("timezone"),
        "latitude": _num(raw.get("lat")),
        "longitude": _num(raw.get("lng")),
        "hours": [{"day": h.get("day"), "open": h.get("open"), "close": h.get("close")} for h in _dig(raw, "hours", "normal") or []],
        "url": raw.get("storePageUrl"),
    }


def _province(code: Any) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    # The store list prefixes the country ("CAON"); show just the province.
    return code[2:] if len(code) == 4 else code


_STOCK_LABELS = {
    "HIGH_IN_STOCK": "in stock",
    "MEDIUM_IN_STOCK": "in stock",
    "LOW_IN_STOCK": "low stock",
    "OUT_OF_STOCK": "out of stock",
}


def _localized_name(names: Any, language: str) -> str | None:
    if isinstance(names, list):
        for n in names:
            if isinstance(n, dict) and n.get("languageCode") == language:
                return n.get("name")
        for n in names:
            if isinstance(n, dict) and n.get("name"):
                return n["name"]
    return None


def sales_location(raw: dict[str, Any], language: str = "en") -> dict[str, Any]:
    out: dict[str, Any] = {"division": raw.get("division")}
    dept = raw.get("department")
    if isinstance(dept, dict):
        out["department"] = _localized_name(dept.get("names"), language) or dept.get("name")
    elif isinstance(dept, str):
        out["department"] = dept
    ab = raw.get("aisleAndBin")
    if isinstance(ab, dict):
        out["aisle"] = ab.get("aisle")
        out["bin"] = ab.get("bin")
    for key in ("aisle", "bin"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    return {k: v for k, v in out.items() if v is not None}


def stock(data: dict[str, Any], stores_by_id: dict[str, dict[str, Any]], language: str = "en") -> list[dict[str, Any]]:
    by_item: dict[str, dict[str, Any]] = {}
    locations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for loc in data.get("salesLocations") or []:
        key = (str(_dig(loc, "itemKey", "itemNo")), str(_dig(loc, "classUnitKey", "classUnitCode")))
        for sl in loc.get("salesLocations") or []:
            if isinstance(sl, dict):
                locations.setdefault(key, []).append(sales_location(sl, language))
    for a in data.get("availabilities") or []:
        item_no = str(_dig(a, "itemKey", "itemNo"))
        entry = by_item.setdefault(
            item_no, {"item_no": item_no, "item_type": _dig(a, "itemKey", "itemType"), "home_delivery": None, "stores": []}
        )
        unit_type = _dig(a, "classUnitKey", "classUnitType")
        code = str(_dig(a, "classUnitKey", "classUnitCode"))
        if unit_type == "RU":
            hd = _dig(a, "buyingOption", "homeDelivery") or {}
            msg = _dig(hd, "availability", "probability", "thisDay", "messageType")
            entry["home_delivery"] = {
                "available": a.get("availableForHomeDelivery"),
                "status": _STOCK_LABELS.get(msg or "", (msg or "").lower().replace("_", " ") or None),
                "click_and_collect": a.get("availableForClickCollect"),
            }
            continue
        if unit_type != "STO":
            continue
        cc = _dig(a, "buyingOption", "cashCarry") or {}
        avail = cc.get("availability") or {}
        if code not in stores_by_id and not avail:
            continue  # pick-up points and distribution centres without store stock
        msg = _dig(avail, "probability", "thisDay", "messageType")
        restocks = [
            {
                "earliest": r.get("earliestDate"),
                "latest": r.get("latestDate"),
                "quantity": r.get("quantity"),
                "reliability": r.get("reliability"),
            }
            for r in avail.get("restocks") or []
        ]
        st = stores_by_id.get(code) or {}
        entry["stores"].append(
            {
                "store_id": code,
                "store": st.get("name") or code,
                "quantity": avail.get("quantity"),
                "status": _STOCK_LABELS.get(msg or "", (msg or "").lower().replace("_", " ") or None),
                "in_range": _dig(cc, "range", "inRange"),
                "cash_and_carry": a.get("availableForCashCarry"),
                "click_and_collect": a.get("availableForClickCollect"),
                "restocks": restocks,
                "updated": avail.get("updateDateTime"),
                "locations": locations.get((item_no, code), []),
            }
        )
    for entry in by_item.values():
        entry["stores"].sort(key=lambda s: str(s["store"]))
    return list(by_item.values())


def fulfilment(data: dict[str, Any]) -> dict[str, Any]:
    prices = data.get("deliveryPrices") or {}
    dates = data.get("deliveryDates") or {}
    items = []
    for it in data.get("items") or []:
        cnc = it.get("cashAndCarry") or {}
        items.append(
            {
                "item_no": it.get("itemNo"),
                "home_delivery": _dig(it, "homeDelivery", "available"),
                "home_delivery_reason": _dig(it, "homeDelivery", "reasonCode"),
                "click_and_collect": _dig(it, "clickAndCollect", "available"),
                "in_store": cnc.get("available"),
                "in_store_quantity": cnc.get("availableQty"),
                "in_store_status": _STOCK_LABELS.get(cnc.get("availabilityLevel") or "", cnc.get("reasonCode")),
                "in_store_restock": _dig(it, "restock", "cashAndCarry"),
                "locations": [sales_location(loc) for loc in cnc.get("salesLocations") or [] if isinstance(loc, dict)],
            }
        )
    return {
        "home_delivery": {
            "price": _num(_dig(prices, "homeDelivery", "current")),
            "original_price": _num(_dig(prices, "homeDelivery", "original")),
            "lead_time_days": _dig(dates, "homeDelivery", "indicativeLeadTime"),
            "parcel": _dig(data, "cart", "isEligibleForParcelDelivery"),
            "truck": _dig(data, "cart", "isEligibleForTruckDelivery"),
        },
        "click_and_collect": {
            "price": _num(_dig(prices, "clickCollect", "current")),
            "original_price": _num(_dig(prices, "clickCollect", "original")),
            "lead_time_days": _dig(dates, "clickCollect", "indicativeLeadTime"),
        },
        "items": items,
    }


# --------------------------------------------------------------------------- cart


def cart(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("cart"), dict) or "cart" in data:
        data = data.get("cart") or {"empty": True}
    items = []
    for group in data.get("groups") or []:
        for it in group.get("items") or []:
            info = it.get("info") or {}
            price = it.get("price") or {}
            unit = _price_pair(_dig(price, "unitPrice", "current"))
            subtotal = _price_pair(_dig(price, "subtotal", "current"))
            items.append(
                {
                    "item_no": it.get("itemNo"),
                    "item_type": it.get("itemType"),
                    "name": info.get("name"),
                    "type_name": info.get("category"),
                    "description": info.get("description"),
                    "measure": _dig(info, "measurements", 0, "metric"),
                    "quantity": _int(it.get("quantity")),
                    "unit_price": unit,
                    "subtotal": subtotal,
                    "family_price": bool(_dig(price, "unitPrice", "isFamilyItem")),
                    "packages": info.get("numberOfPackages"),
                    "children": [{"item_no": c.get("itemNo"), "quantity": _int(c.get("quantity"))} for c in it.get("childItems") or []],
                }
            )
    total = _dig(data, "price", "totalPrice") or {}
    current = total.get("current") or {}
    return {
        "empty": bool(data.get("empty", not items)),
        "lines": len(items),
        "quantity": _int(data.get("quantity")) or sum(i["quantity"] or 0 for i in items),
        "items": items,
        "discount_codes": [(c.get("code") if isinstance(c, dict) else c) for c in data.get("discountCodes") or [] if c],
        "total": _price_pair(current),
        "total_excl_tax": _num(current.get("exclTax")),
        "total_incl_tax": _num(current.get("inclTax")),
        "savings": _price_pair(_dig(total, "savings")) if isinstance(total.get("savings"), dict) else None,
        "currency": _dig(data, "price", "currency"),
        "family_member": _dig(data, "customer", "familyLoyalty"),
    }


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else round(n)


# --------------------------------------------------------------------------- favourites


def fav_list(raw: dict[str, Any]) -> dict[str, Any]:
    items = []
    for it in raw.get("items") or []:
        product = it.get("product") or {}
        regular = _dig(it, "prices", "online", "regular", "unit")
        items.append(
            {
                "item_no": it.get("itemNo"),
                "item_type": it.get("type"),
                "name": product.get("name") or product.get("globalName"),
                "description": product.get("description"),
                "quantity": it.get("quantity"),
                "unit_price": _price_pair(regular),
                "buyable_online": it.get("isBuyableOnline"),
                "added_at": it.get("addedAt"),
            }
        )
    return {
        "list_id": raw.get("listId"),
        "name": raw.get("name") or "",
        "selected": bool(raw.get("isSelected")),
        "updated": raw.get("updated"),
        "quantity": raw.get("quantity"),
        "items": items,
    }


# --------------------------------------------------------------------------- orders


def _date(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    date = obj.get("date")
    time_ = obj.get("time")
    if date and time_:
        return f"{date}T{time_}"
    return date


def purchase_summary(p: dict[str, Any]) -> dict[str, Any]:
    articles = p.get("articles") or {}
    return {
        "id": p.get("id"),
        "type": (p.get("type") or "").lower() or None,
        "status": p.get("status"),
        "event_status": p.get("eventStatus"),
        "date": _date(p.get("dateAndTime")),
        "store": p.get("storeName"),
        "total": _num(_dig(p, "totalCost", "value")),
        "total_formatted": _dig(p, "totalCost", "formatted"),
        "currency": _dig(p, "totalCost", "code"),
        "item_count": articles.get("quantity"),
        "items_preview": [a.get("name") for a in articles.get("any") or [] if isinstance(a, dict)],
    }


def _money(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    return {"value": _num(obj.get("value")), "formatted": obj.get("formatted"), "currency": obj.get("code")}


def delivery_methods(methods: Any) -> list[dict[str, Any]]:
    out = []
    for m in methods or []:
        statuses = m.get("statusV2") or []
        current = next((s for s in statuses if s.get("tense") == "PRESENT"), statuses[-1] if statuses else {})
        dd = m.get("deliveryDate") or {}
        out.append(
            {
                "type": m.get("type"),
                "status": current.get("deliveryStatusV2"),
                "timeline": [{"status": s.get("deliveryStatusV2"), "tense": s.get("tense")} for s in statuses],
                "delivered_at": _date(dd.get("actual")),
                "estimated_from": _date(dd.get("estimatedFrom")),
                "estimated_to": _date(dd.get("estimatedTo")),
                "window": dd.get("dateTimeRange"),
            }
        )
    return out


def purchase_detail(p: dict[str, Any], kind: str) -> dict[str, Any]:
    costs = p.get("costs") or {}
    articles = p.get("articles") or {}
    return {
        "id": p.get("id"),
        "type": kind,
        "status": p.get("status"),
        "date": _date(p.get("dateAndTime")),
        "store": p.get("storeName"),
        "costs": {k: _money(costs.get(k)) for k in ("sub", "delivery", "service", "discount", "tax", "total") if costs.get(k)},
        "items": [
            {
                "item_no": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "quantity": a.get("quantity"),
                "unit_price": _dig(a, "unitPrice", "formatted"),
                "total_price": _dig(a, "totalPrice", "formatted"),
                "url": a.get("href"),
            }
            for a in articles.get("any") or []
            if isinstance(a, dict)
        ],
        "deliveries": delivery_methods(p.get("deliveryMethods")),
    }


# --------------------------------------------------------------------------- loyalty


def mask(value: Any, keep: int = 4) -> str | None:
    if not value:
        return None
    s = str(value)
    return "•" * max(0, len(s) - keep) + s[-keep:]
