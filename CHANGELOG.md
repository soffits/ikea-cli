# Changelog

## 0.2.0 (2026-10-22)

Works in every IKEA country that shares the www.ikea.com platform, not only Canada.

- New `ikea markets [QUERY] [--all]`: countries, languages, currency, whether login is available, and when each market was last checked live. `--all` also lists countries whose IKEA website is separate and not supported.
- Market data (languages, default language, currency, price format, login settings) comes from IKEA's own pages and is shipped as `markets.json`; `scripts/update_markets.py` regenerates it.
- Choosing a country that runs its own IKEA website (for example China or Türkiye), a code that isn't an IKEA market, or a language a market doesn't offer now fails with a clear message (exit 7, `unsupported_market`).
- `ikea config set country CODE` picks the market's main language when the current one isn't offered there, and drops the default store and postal code of the previous market.
- Login uses each market's own IKEA accounts site and public client id. Markets whose website signs in another way say so; their guest features still work.
- Prices are written the way IKEA writes them in the market and language (`$1,299.00`, `1 299,00 $`, `¥12,990`, `CHF 3'540.25`).
- Delivery works in markets where IKEA has no postal-code lookup; the postal code is passed on as given.
- Markets where IKEA runs the cart, favourites, delivery check or sign-in on another system say so up front (exit 5, `not_available`; `ikea markets` lists what isn't available) instead of failing with HTTP 401.
- A new exit-5 error code `not_available` and config error codes `market_not_set` and `unsupported_market` (exit 7).
- Store regions drop the country prefix for every market (`GBABE` -> `ABE`).
- Examples, help text and test data no longer refer to real places.
- Live tests and `scripts/live-smoke.sh` take a market and pick products, a store and a postal code from IKEA's data for it.

Changes for existing users:

- A new install no longer defaults to Canada: commands that need a market ask you to pick one (exit 7, `market_not_set`). Setups from 0.1 (an existing config file, a Canadian session or guest cart) keep using Canada; the CLI writes `country = ca` to the config once and says so.
- `Settings()` in the library no longer defaults to `country="ca"`; pass `Settings(country=...)`. `IkeaClient` raises `MarketNotSet` or `UnsupportedMarket` when created without a usable market.
- `config.KNOWN_AUTH` and `config.VERIFIED_MARKETS` were removed; use `ikea_cli.markets`.
- `output.money()` no longer assumes `$` for CAD/USD/AUD; pass the market's `PriceFormat`, or use `IkeaClient.format_price()`.
- The `stores` table column `Prov.` is now `Region`.

## 0.1.0 (2026-10-22)

First release.

- Search with filters, sort, paging, store and postal-code context; category browsing; suggestions.
- Product details: price, rating, dimensions, packages, materials, care, variants, images.
- Stores, default store, per-store stock with restock dates and aisle/bin locations.
- Delivery and click & collect options for a postal code.
- Cart: show, add (also from a file), set quantity, remove, clear, discount codes. Guest cart when not logged in.
- Favourites lists: list, show, create, rename, delete, select, add, set, remove.
- Purchases: list, details, delivery status.
- IKEA Family membership, points and rewards; profile.
- Login through the browser with a copy-paste step (no password in the terminal), session import, automatic token refresh, logout with revoke.
- `--json` output with stable exit codes; agent skill in `skills/ikea/SKILL.md`.
