#!/usr/bin/env python3
"""Regenerate src/ikea_cli/markets.json from IKEA's own public pages.

    uv run --with pycountry scripts/update_markets.py            # all markets
    uv run --with pycountry scripts/update_markets.py --only us,de

Sources, all public and the same ones the IKEA website loads in a browser:

- https://www.ikea.com/: its `hreflang` links list every country and language served at
  https://www.ikea.com/{cc}/{lang}/, the shared IKEA platform this tool talks to.
- https://www.ikea.com/global/en/shared-data/regions.js: the country picker on www.ikea.com,
  with local names and the countries whose IKEA website is separate (franchise or regional).
- https://www.ikea.com/{cc}/: redirects to the market's default language.
- https://www.ikea.com/{cc}/{lang}/profile/login/ and the profile app it loads: the market's
  public login settings (Auth0 domain and client id).
- One product search per market and language: the currency and how IKEA writes prices there.

pycountry (LGPL-2.1) is only used here, for English country names; the package does not need it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "ikea_cli" / "markets.json"
REGIONS_URL = "https://www.ikea.com/global/en/shared-data/regions.js"
SEARCH_VERSION = "20260727"
PLATFORM_RE = re.compile(r"^https://www\.ikea\.com/([a-z]{2})/([a-z]{2})/$")
AUTH0_RE = re.compile(r'AUTH0:\{DOMAIN:"([a-z0-9.-]+)",CLIENT_ID:"([A-Za-z0-9_-]+)"\}')
PAUSE = 0.4

http = httpx.Client(
    timeout=60,
    follow_redirects=False,
    headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64) ikea-cli market table generator"},
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get(url: str, **kw: Any) -> httpx.Response:
    for attempt in range(3):
        try:
            resp = http.get(url, **kw)
            time.sleep(PAUSE)
            return resp
        except httpx.TransportError as exc:
            log(f"  retry {url}: {exc.__class__.__name__}")
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"cannot reach {url}")


def english_name(code: str, keywords: list[str]) -> str:
    try:
        import pycountry
    except ImportError:  # names fall back to IKEA's own keywords
        return keywords[0] if keywords else code.upper()
    country = pycountry.countries.get(alpha_2=code.upper())
    if country is None:
        return keywords[0] if keywords else code.upper()
    return getattr(country, "common_name", None) or country.name


HREFLANG_RE = re.compile(r'hreflang="([a-z]{2})-([A-Za-z]{2})" href="https://www\.ikea\.com/([a-z]{2})/([a-z]{2})/"')


def load_platform() -> dict[str, list[str]]:
    """Country -> languages, from the hreflang links on www.ikea.com (IKEA's own order)."""

    out: dict[str, list[str]] = {}
    for _hl_lang, _hl_cc, cc, lang in HREFLANG_RE.findall(get("https://www.ikea.com/").text):
        if lang not in out.setdefault(cc, []):
            out[cc].append(lang)
    return out


def load_regions() -> dict[str, Any]:
    text = get(REGIONS_URL).text
    return json.loads(text[text.index("=") + 1 :].strip().rstrip(";"))


def default_language(cc: str, languages: list[str]) -> str:
    resp = get(f"https://www.ikea.com/{cc}/")  # no Accept-Language, so IKEA picks its own default
    location = str(resp.url.join(resp.headers.get("location", ""))) if resp.is_redirect else ""
    m = PLATFORM_RE.match(location)
    if m and m.group(1) == cc and m.group(2) in languages:
        return m.group(2)
    local = [lang for lang in languages if lang != "en"]
    return local[0] if local else languages[0]


def auth_settings(cc: str, lang: str) -> dict[str, str] | None:
    page = get(f"https://www.ikea.com/{cc}/{lang}/profile/login/")
    m = re.search(rf"/{cc}/{lang}/profile/app-[A-Za-z0-9_-]+\.js", page.text)
    if page.status_code != 200 or not m:
        log(f"  {cc}/{lang}: no profile app found (HTTP {page.status_code})")
        return None
    bundle = get("https://www.ikea.com" + m.group(0)).text
    found = AUTH0_RE.search(bundle)
    if not found:
        log(f"  {cc}/{lang}: no AUTH0 settings in the profile app")
        return None
    return {"domain": found.group(1), "client_id": found.group(2)}


def _search(cc: str, lang: str, sort: str, text: str) -> list[dict[str, Any]]:
    body = {
        "searchParameters": {"input": text, "type": "QUERY"},
        "components": [
            {"component": "PRIMARY_AREA", "types": {"main": "PRODUCT", "breakouts": []}, "window": {"size": 24, "offset": 0}, "sort": sort}
        ],
    }
    resp = http.post(
        f"https://sik.search.blue.cdtapps.com/{cc}/{lang}/search",
        params={"c": "sr", "v": SEARCH_VERSION},
        content=json.dumps(body),
        headers={"content-type": "text/plain;charset=UTF-8"},
    )
    time.sleep(PAUSE)
    if resp.status_code != 200:
        return []
    primary = next((r for r in resp.json().get("results") or [] if r.get("component") == "PRIMARY_AREA"), {})
    return [i["product"] for i in primary.get("items") or [] if i.get("product")]


def price_format(cc: str, lang: str) -> tuple[str | None, dict[str, Any] | None]:
    """Learn currency, symbol position, grouping and decimals from prices IKEA shows."""

    # Product names are the same in every language: a large range for big prices (grouping), and
    # small items for decimals.
    products = (
        _search(cc, lang, "PRICE_HIGH_TO_LOW", "PAX")
        + _search(cc, lang, "PRICE_LOW_TO_HIGH", "GLIMMA")
        + _search(cc, lang, "PRICE_LOW_TO_HIGH", "FRAKTA")
    )
    currency = None
    fmt: dict[str, Any] = {"prefix": "", "suffix": "", "decimals": 0, "group": "", "decimal": "."}
    seen = False
    for p in products:
        sp = p.get("salesPrice") or {}
        cur = sp.get("current") or {}
        if not cur.get("wholeNumber"):
            continue
        seen = True
        currency = currency or sp.get("currencyCode")
        fmt["prefix"] = fmt["prefix"] or (cur.get("prefix") or "")
        fmt["suffix"] = fmt["suffix"] or (cur.get("suffix") or "")
        group = re.sub(r"\d", "", cur["wholeNumber"])
        if group and not fmt["group"]:
            fmt["group"] = group[0]
        decimals = cur.get("decimals") or ""
        if decimals.isdigit() and len(decimals) > fmt["decimals"]:
            fmt["decimals"] = len(decimals)
            fmt["decimal"] = cur.get("separator") or "."
    if not seen:
        return None, None
    if not fmt["group"]:
        # No price was large enough to show grouping; use a space if the decimal mark is a comma, else a comma.
        fmt["group"] = "\u00a0" if fmt["decimal"] == "," else ","
    if fmt["decimals"] and fmt["group"] == fmt["decimal"]:
        # Some markets send the same mark for both (e.g. "3.685.00"); drop grouping rather than be ambiguous.
        fmt["group"] = ""
    return currency, fmt


def build(only: set[str] | None) -> dict[str, Any]:
    regions = load_regions()
    platform_langs = load_platform()
    lang_names = {k.lower(): v.get("language") for k, v in (regions.get("langByCode") or {}).items()}
    previous = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    markets: dict[str, Any] = dict(previous.get("markets") or {}) if only else {}
    other: dict[str, Any] = {}
    for code, region in sorted((regions.get("regionByCode") or {}).items()):
        cc = code.lower()
        if not re.fullmatch(r"[a-z]{2}", cc):
            continue
        keywords = [k.strip() for k in str(region.get("keywords") or "").split(",") if k.strip()]
        urls = {k.lower(): v for k, v in (region.get("siteUrlByLangCode") or {}).items()}
        platform = {lang: f"https://www.ikea.com/{cc}/{lang}/" for lang in platform_langs.get(cc, [])}
        for lang, url in urls.items():
            m = PLATFORM_RE.match(url)
            if m and m.group(1) == cc and lang not in platform:
                platform[lang] = url
        name = english_name(cc, keywords)
        if not platform:
            target = next(iter(urls.values()), None)
            m = PLATFORM_RE.match(target or "")
            entry: dict[str, Any] = {"name": name, "url": target}
            if m:
                entry["served_by"] = m.group(1)  # e.g. Luxembourg is served by the Belgian site
            other[cc] = entry
            continue
        if only and cc not in only:
            continue
        log(f"{cc}: {', '.join(platform)}")
        languages = sorted(platform)
        default = default_language(cc, languages)
        auth = auth_settings(cc, default)
        currency = None
        formats: dict[str, Any] = {}
        for lang in languages:
            cur, fmt = price_format(cc, lang)
            currency = currency or cur
            if fmt:
                formats[lang] = fmt
        markets[cc] = {
            "name": name,
            "keywords": keywords,
            "languages": {lang: lang_names.get(lang) or lang for lang in languages},
            "default_language": default,
            "currency": currency,
            "price_format": formats,
            "auth": auth,
        }
    return {
        "generated": dt.date.today().isoformat(),
        "source": "scripts/update_markets.py",
        "markets": dict(sorted(markets.items())),
        "other_sites": dict(sorted(other.items())) if not only else previous.get("other_sites", other),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated country codes to refresh; others are kept from the current file")
    ap.add_argument("--output", type=Path, default=OUT)
    args = ap.parse_args()
    only = {c.strip().lower() for c in args.only.split(",")} if args.only else None
    data = build(only)
    args.output.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"wrote {args.output}: {len(data['markets'])} markets, {len(data['other_sites'])} other sites")


if __name__ == "__main__":
    main()
