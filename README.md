# ikea-cli

A command-line tool and Python library for the IKEA website. Search the catalogue, read product details, check store stock and delivery options, manage your real shopping cart and favourites lists, and look up your orders and IKEA Family points. The only thing you can't do with it is pay: checkout stays on ikea.com.

It works for people at a terminal and for scripts and AI agents: every command can print JSON (`--json`), and exit codes are stable.

> **Unofficial.** ikea-cli is an independent project. It is not made, endorsed or supported by IKEA or Inter IKEA Systems B.V. It uses the same web services your browser uses on ikea.com, which can change without notice. "IKEA" is a trademark of Inter IKEA Systems B.V.

**Markets:** every country whose IKEA shop lives at `www.ikea.com/<country>/<language>/` (about 50, from the United States and Germany to Japan and Saudi Arabia). `ikea markets` lists them. Countries with their own separate IKEA website (for example China, Türkiye or Greece) are not supported, and the tool says so. See [Markets](#markets) for what was checked where. Account features (login, your cart and lists, orders, points) have only been tested with a Canadian account.

## Install

Python 3.11 or newer is required.

```bash
# with uv
uv tool install ikea-cli

# or with pipx
pipx install ikea-cli

# latest code from GitHub
uv tool install git+https://github.com/soffits/ikea-cli
```

This installs the `ikea` command. Check it with `ikea --version`.

## Quick start

No account is needed to search, check stock or try the cart. Without a login, the cart and lists live in a guest session on this computer.

First pick your country (and, where IKEA offers several, your language):

```console
$ ikea markets
Code  Country               Languages           Currency  Not available                 Tested
----  --------------------  ------------------  --------  ----------------------------  ----------
au    Australia             en                  AUD       -                             2026-10-22
at    Austria               de, en              EUR       -                             -
bh    Bahrain               ar, en              BHD       login, cart, lists, delivery  -
...
$ ikea config set country de
Set country = de
$ ikea store set <store-id>          # a store id or a unique part of its name, from `ikea stores`
Default store: IKEA <store name> (<store-id>)
```

The examples below were taken in Canada (English); other markets show their own currency, language and stores.

```console
$ ikea search desk --sort price-asc -n 3 -f colors=10139
Item        Name               Description                                             Price  Rating
----------  -----------------  ----------------------------------------------------  -------  ----------
102.447.43  MICKE              Desk, black-brown, 105x50 cm (41 3/8x19 5/8 ")        $149.00  4.5 (6322)
604.735.48  ALEX               Drawer unit, black-brown, 36x70 cm (14 1/8x27 1/2 ")  $129.00  4.5 (3763)
394.176.96  ANFALLARE / ADILS  Desk, bamboo/black, 140x65 cm (55 1/8x25 5/8 ")       $140.00  4.9 (23)
1-3 of 13  (next: --page 2)   Filters: add --filters

$ ikea stock 304.499.08 106.232.20
106.232.20  home delivery: in stock
Store              Qty  Status        Restock           Location
-----------------  ---  ------------  ----------------  --------
IKEA <store name>    0  out of stock  40 on 2026-10-22  Rugs

304.499.08  home delivery: in stock
Store              Qty  Status    Restock  Location
-----------------  ---  --------  -------  ---------------
IKEA <store name>   81  in stock  -        Activity area 4

$ ikea cart add 304.499.08:2 106.232.20
Added 3 item(s).

Guest cart: 2 lines, 3 items

Item        Name      Description                            Qty    Each  Subtotal
----------  --------  -------------------------------------  ---  ------  --------
304.499.08  LACK      side table, white, 55x55 cm              2  $18.99    $37.98
106.232.20  LOKALTÅG  rug, low pile, beige/gray, 160x230 cm    1  $89.99    $89.99

Total (before tax): $127.97
```

Item numbers can be written as `30449908`, `304.499.08`, or pasted as a product URL. Combination products (the ones whose URL ends in `s` plus digits) work too.

## Logging in

Log in when you want your real IKEA cart, your favourites lists, your orders or your IKEA Family points.

IKEA's login page is protected by a browser check (Cloudflare Turnstile), so no command-line program can fill in the login form. ikea-cli never asks for your password. You sign in on ikea.com in your normal browser, and then copy one page back into the terminal:

```bash
ikea auth login
```

It prints these steps:

1. Sign in at `https://www.ikea.com/<country>/<language>/profile/login/` in your browser (the command prints the right address for your market) and tick **Keep me signed in**. Skip this if you are already signed in.
2. In the same browser, open the link that `ikea auth login` printed, with `view-source:` in front of it. In Safari, open the link normally and use **Develop › Show Page Source**.
3. Select all (Ctrl+A or Cmd+A), copy, and paste into the terminal. The page is about 2 KB and contains a one-time code.

ikea-cli then swaps the code for its own tokens. Access tokens last 24 hours and are renewed automatically with a refresh token, so you normally log in once. The tokens are stored in a file only you can read (see [Files](#files)) and are never printed.

```console
$ ikea auth status
Logged in as  Alex
Market        Canada (ca)
Loyalty       FAMILYCA
Access token  valid for 23 h 41 min
Auto-refresh  yes
Source        login
```

Other ways to log in:

| Situation | Command |
| --- | --- |
| Two-step login, for scripts or agents | `ikea auth login --start` prints the link, then `ikea auth login --finish -` reads the pasted page from stdin. |
| You prefer to copy the ikea.com address (experimental) | `ikea auth login --redirect`: sign in through the link, then copy the `https://www.ikea.com/…/profile/login/#code=…` address. The IKEA page moves on quickly; the address is still in your browser history. This variant has not been tested end to end yet; use the default flow if it fails. |
| A server with no browser | Copy the `auth0` cookie for your market's accounts site (`<country>.accounts.ikea.com`) from your browser's developer tools (Application/Storage › Cookies), then run `ikea auth import --cookie -` and paste it. The cookie is used once and not stored. |
| You already have an access token | `ikea auth import --access-token -` (valid for the rest of its 24 hours, cannot be renewed), or set `IKEA_ACCESS_TOKEN` for a single run. |

Login works per market, with the market's own IKEA accounts site. In some markets the IKEA website signs in through a different system that ikea-cli can't use; there `ikea auth login` says so, and everything that works without an account still works.

`ikea auth logout` revokes the refresh token at IKEA and deletes the local session. The guest session (guest cart and lists) is kept; add `--forget-guest` to drop it too. `ikea auth refresh` renews the access token on demand.

## Commands

Run `ikea COMMAND --help` for every option.

### Catalogue

```bash
ikea search "billy bookcase"              # text search
ikea search desk --filters                # list the filters and sorts available for this search
ikea search desk -f colors=10156 -f price-buckets=PRICE_10000_15000 --sort rating
ikea search desk -f new-product           # yes/no filters need no value
ikea search desk -n 50 --page 2           # paging
ikea categories rug                       # find category ids
ikea search --category 10653              # browse a category
ikea search white --category 10653        # search inside a category
ikea suggest kal                          # search suggestions
ikea product 106.232.20                   # price, rating, dimensions, packages, materials, variants
ikea product https://www.ikea.com/<country>/<language>/p/<product>-s09563608/ --quick
```

Filter names and value ids come from `--filters`; they differ per search. The `f-` prefix IKEA uses is optional. `--filters` also lists the sort orders IKEA offers; `--sort` takes the short names (`price-asc`, `rating`, …) or any of those ids.

```console
$ ikea product 106.232.20
Item         106.232.20
Name         LOKALTÅG
Type         rug, low pile
Size         160x230 cm (5 ' 3 "x7 ' 7 ")
Price        $89.99
Rating       4.6 / 5 (73 reviews)
...
Packages (1)
Package                 Item      Qty  Width           Height        Length          Weight
----------------------  --------  ---  --------------  ------------  --------------  --------------------
LOKALTÅG Rug, low pile  10623220  1    40 cm (15 ½ ")  8 cm (3 ¼ ")  57 cm (22 ¼ ")  3.96 kg (8 lb 12 oz)
...
Choose size: 4 ' 4 "x6 ' 5 " [40623228], 5 ' 3 "x7 ' 7 " [10623220] *, 6 ' 7 "x9 ' 10 " [00623230]
```

### Stores, stock and delivery

```bash
ikea stores                     # all stores; * marks your default
ikea store set <store-id>       # or a unique part of the name
ikea store show                 # address and opening hours
ikea stock 304.499.08           # your default store (or all stores if none is set)
ikea stock 304.499.08 --all     # every store, with restock dates and aisle/bin locations
ikea delivery 304.499.08:2 106.232.20 --postal-code "<postal-code>"
ikea delivery                   # uses the items in your cart
```

```console
$ ikea delivery 30449908:2 10623220 --postal-code "<postal-code>"
Postal code <postal-code> (<city>, <region>)
Home delivery:     $19.99, about 28 - 31 days
Click & collect:   $5.00, about 1 - 2 days

Item        Delivery  Collect  In store  Restock
----------  --------  -------  --------  ----------
304.499.08  yes       yes      yes (81)  -
106.232.20  yes       yes      no (0)    2026-10-22
```

Delivery prices and dates are IKEA's estimates for the address; the final price is set at checkout. Only `delivery` needs a postal code. Where IKEA has no postal-code lookup for a country, the code is passed on as you typed it and city and region stay empty.

### Cart

When you are logged in, these commands change **your real IKEA cart**, the same one you see on ikea.com and in the app. Use `--guest` to work on a separate guest cart instead.

```bash
ikea cart                           # same as `ikea cart show`
ikea cart add 304.499.08:2 30449908x1
ikea cart add --file shopping.txt   # lines of "item quantity", or a JSON list; "-" reads stdin
ikea cart set 304.499.08 4          # set an exact quantity; 0 removes
ikea cart set 30449908:4 10623220:1
ikea cart remove 304.499.08
ikea cart coupon add CODE
ikea cart coupon remove CODE
ikea cart clear --yes
```

Totals are shown the way ikea.com shows them in your market: before tax in some (for example Canada and the United States), with tax in most others.

### Favourites lists

```bash
ikea lists                          # all lists
ikea lists show                     # items in your selected list
ikea lists show "Living room"
ikea lists create "Living room"
ikea lists add 304.499.08:2 --list "Living room"
ikea lists set 304.499.08:1 --list "Living room"
ikea lists remove 304.499.08 --list "Living room"
ikea lists rename "Living room" "Den"
ikea lists select "Den"
ikea lists delete "Den" --yes
```

### Orders, IKEA Family and rewards (login required)

```bash
ikea orders                     # online orders and store receipts
ikea orders show 123456789      # items, costs and delivery details
ikea orders status 123456789    # delivery progress
ikea loyalty                    # IKEA Family membership and points
ikea rewards                    # rewards you can redeem, and rewards you have claimed
ikea profile
```

ikea-cli only reads orders and rewards. It cannot place, change or cancel orders, and it does not spend points.

## For scripts and agents

Add `--json` to any command (before or after the command name). Success prints `{"ok": true, "data": ...}` to stdout. Errors print `{"ok": false, "error": {"code": ..., "message": ..., "hint": ...}}` to stdout and exit with a non-zero code:

| Exit code | Meaning | `error.code` |
| --- | --- | --- |
| 0 | success | |
| 1 | unexpected error (set `IKEA_DEBUG=1` to see the details) | `error` |
| 2 | bad command line or input | `usage` |
| 3 | not logged in, session expired, or login failed | `auth_required`, `session_expired`, `login_failed` |
| 4 | not found (item, store, list, order) | `not_found` |
| 5 | IKEA rejected the request, or the feature isn't offered in this market | `api_error` (with `status` and `details.codes`), `not_available` |
| 6 | network problem | `network` |
| 7 | bad configuration, no market chosen, or a market ikea-cli can't use | `config`, `market_not_set`, `unsupported_market` |

Commands that would ask a question (`cart clear`, `lists delete`) need `--yes` when not run in a terminal. An agent skill describing the tool is in [`skills/ikea/SKILL.md`](skills/ikea/SKILL.md) and is also installed with the package.

### Python library

```python
from ikea_cli import IkeaClient

from ikea_cli import Settings

with IkeaClient(Settings(country="de")) as ikea:  # or IkeaClient() to use your saved settings
    results = ikea.search("kallax", limit=5, sort="price-asc")
    for item in results["items"]:
        print(item["item_no"], item["name"], ikea.format_price(item["price"]))

    stock = ikea.stock(["30449908"], store="<store-id>")
    cart = ikea.cart_add([("30449908", 2)])
```

`IkeaClient` uses the same settings and session as the command. It raises `MarketNotSet` when no country is configured and `UnsupportedMarket` for a country it can't serve. `ikea_cli.markets` has the market table. Methods return plain dictionaries (the same data as `--json`) and raise `ikea_cli.IkeaError` subclasses.

## Configuration

```bash
ikea config show
ikea config set country us              # your market (see `ikea markets`)
ikea config set language es             # one of the market's languages
ikea config set postal_code "<postal-code>"   # used for delivery and search context
ikea config path                        # where files are kept
```

| Setting | Meaning | Environment variable |
| --- | --- | --- |
| `country` | market (two-letter code from `ikea markets`); no default | `IKEA_COUNTRY` |
| `language` | language; default: the market's main language | `IKEA_LANGUAGE` |
| `store` | default store id | `IKEA_STORE` |
| `postal_code` | default postal code | `IKEA_POSTAL_CODE` |
| `auth_domain`, `auth_client_id` | override the market's login settings (only needed if IKEA changes them before ikea-cli is updated) | |

`--country` and `--language` override the settings for one command. Changing the country drops the default store and postal code, which belong to one market. Settings from version 0.1 without a country keep working with Canada; the first run writes `country = ca` to the config and says so.

### Files

Settings, session and cache follow your platform's conventions (on Linux: `~/.config/ikea-cli`, `~/.local/state/ikea-cli`, `~/.cache/ikea-cli`). Set `IKEA_CLI_HOME` to keep everything in one folder instead. Session files are written atomically with permissions `0600` in a `0700` folder. Logging in again or `ikea auth logout` replaces or removes them. Commands that renew the session take a lock first, so running several `ikea` commands at once is safe (on Windows the lock uses `msvcrt`; Windows is not covered by the automated tests yet).

## Markets

IKEA runs a separate shop per country, each with its own prices, stores, item range and account. ikea-cli works with the countries IKEA serves from `www.ikea.com/<country>/<language>/`:

```bash
ikea markets            # code, country, languages, currency, what isn't available, last live check
ikea markets --all      # also the countries ikea-cli can't use
ikea markets swe        # find a country by name
```

What a market offers comes from IKEA itself. The table in `src/ikea_cli/markets.json` is generated by `scripts/update_markets.py` from IKEA's country picker, each market's login settings and a few read-only guest requests, so it can be refreshed when IKEA changes something.

Not every feature exists everywhere:

- Countries with their own IKEA website (for example China, Türkiye, Greece, Taiwan, Iceland, the Spanish islands) are not supported; choosing one gives a clear error.
- In some markets (for example the United Arab Emirates, Saudi Arabia, Mexico or Singapore) IKEA runs the cart, favourites, delivery check and sign-in on a different system. There ikea-cli does search, product details, stores and stock, and says plainly that the rest isn't available.
- Where IKEA has no postal-code lookup (for example South Korea), delivery still works; city and region are just left empty.

Checked live with a guest session on 2026-10-22 (search, suggestions, categories, product details, stores, stock, delivery, guest cart, guest lists, and that IKEA's login service accepts the login link):

| Market | Search, product, stores, stock | Delivery | Guest cart | Guest lists | Login link |
| --- | --- | --- | --- | --- | --- |
| Canada (`ca`, en and fr) | yes | yes | yes | yes | yes |
| United States (`us/en`) | yes | yes | yes | yes | yes |
| United Kingdom (`gb/en`) | yes | yes | yes | yes | yes |
| Germany (`de/de`) | yes | yes | yes | yes | yes |
| France (`fr/fr`) | yes | yes | yes | yes | yes |
| Sweden (`se/sv`) | yes | yes | yes | yes | yes |
| Spain (`es/es`) | yes | yes | yes | yes | yes |
| Japan (`jp/ja`) | yes | yes | yes | yes | yes |
| South Korea (`kr/ko`) | yes | yes (no postal-code lookup) | yes | yes | yes |
| Australia (`au/en`) | yes | yes | yes | yes | yes |
| United Arab Emirates (`ae/en`) | yes | not offered | not offered | not offered | not offered |

The other markets in `ikea markets` use the same services and settings from the same generator, but were not run end to end.

Account features (login, your cart and lists, orders, IKEA Family) have only been tested with a Canadian account; other markets use the same services with their own login settings.

## Troubleshooting

**"This browser is not signed in to IKEA."** The pasted page says `login_required`. Sign in on ikea.com in the same browser, then open the `view-source:` link again. Private windows don't share the sign-in with normal windows.

**"The pasted code belongs to a different login attempt."** You opened a link from an older `ikea auth login` run. Use the most recent link; each run makes a new one.

**"IKEA rejected the login code."** Codes work once and only for a few minutes. Run `ikea auth login` again.

**Why not just log in from the terminal?** IKEA's login form runs a Cloudflare Turnstile check that only a real browser passes. Everything after login is ordinary web requests, so the browser is only needed for the sign-in itself.

**"Your IKEA session has expired."** The refresh token stopped working (after a long time unused, after `ikea auth logout`, or after a password change), or IKEA rejected the session (HTTP 401). Log in again.

**"Your saved IKEA session could not be read."** The session file is damaged. Run `ikea auth login`, or `ikea auth logout --local-only` to remove it.

**"Could not reach IKEA to renew your session."** Try again. If the next attempt says the session expired, IKEA renewed it but the answer never arrived; log in again.

**The cart shows different items than ikea.com.** Check `ikea auth status`. When not logged in, or with `--guest`, you see the guest cart stored on this computer.

**No delivery price.** For some small orders IKEA doesn't offer home delivery on its own; the item list still shows what can be delivered.

**Something that used to work returns HTTP 4xx/5xx.** IKEA may have changed its website. Please open an issue with the command, the `--json` output, and the date. Leave out personal data.

## Development

```bash
git clone https://github.com/soffits/ikea-cli
cd ikea-cli
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest                       # offline tests only
IKEA_LIVE=1 uv run pytest -m live   # optional checks against the real website (guest only)
```

Tests never touch the network or your own session unless you opt in. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for how the code is laid out.

## Acknowledgements

Earlier community projects showed what was possible and helped confirm how IKEA's services behave: [vrslev/ikea-api-client](https://github.com/vrslev/ikea-api-client) (MIT), [Ephigenia/ikea-availability-checker](https://github.com/Ephigenia/ikea-availability-checker) (MIT) and [idelsink/ikea-openapi](https://github.com/idelsink/ikea-openapi). ikea-cli contains no code from them.

## License

Copyright © 2026 Sakina. Licensed under the [GNU Affero General Public License v3.0 only](LICENSE).
