---
name: ikea
description: Use the `ikea` command-line tool to search IKEA products, read product details, check store stock and delivery options, manage the user's IKEA shopping cart and favourites lists, and read their orders and IKEA Family points. Use when the user asks about IKEA products, prices, availability, their IKEA cart, wish lists, orders or rewards. Cannot check out or pay.
---

# IKEA via `ikea`

`ikea` is an unofficial CLI for the IKEA website (Canada tested; country/language are settings). Always pass `--json` and read the envelope:

- success: `{"ok": true, "data": ...}`, exit code 0
- failure: `{"ok": false, "error": {"code", "message", "hint"?, "status"?, "details"?}}`, exit codes: 1 unexpected, 2 usage, 3 auth (`auth_required`, `session_expired`, `login_failed`), 4 not found, 5 IKEA rejected the request, 6 network, 7 config.

On an error, read `error.hint`; it says what to run next. Don't retry exit codes 2-5 unchanged.

## Before you start

```bash
ikea --json config show        # country, language, default store, postal code
ikea --json auth status        # logged_in: true = cart/lists are the user's real account
```

If the user has a preferred store or postal code, save it: `ikea store set "north york"`, `ikea config set postal_code "M5V 3L9"`.

## Item numbers

Accepted forms: `30449908`, `304.499.08`, `s09563608` (combination product), or a product URL. Quantities: `ITEM:QTY` (e.g. `304.499.08:2`). Results use 8-digit `item_no`.

## Catalogue (no login needed)

```bash
ikea --json search "desk" -n 10 --sort price-asc            # short sorts: relevance price-asc price-desc newest rating name popular discount width height depth length; raw ids from --filters also work
ikea --json search "desk" --filters                          # data.filters[]: {parameter, name, values[{id,name,count}]}
ikea --json search "desk" -f colors=10139 -f price-buckets=PRICE_10000_15000 -f new-product
ikea --json search "desk" -n 24 --page 2                     # data.total, data.offset, data.count
ikea --json categories sofa                                  # [{id, name, url}]
ikea --json search --category 10653                          # browse a category
ikea --json search "white" --category 10653                  # search inside a category
ikea --json suggest kal
ikea --json product 106.232.20                               # dimensions, packages, materials, variants[].options[].item_no, images
ikea --json product 106.232.20 --quick                       # summary only, one small request
```

Filter ids differ per search; get them from `--filters` (drop the `f-` prefix or keep it).

## Stock and delivery (no login needed)

```bash
ikea --json stores
ikea --json stock 30449908 10623220            # default store, or all stores if none set
ikea --json stock 30449908 --store 149         # data[].stores[]: quantity, status, restocks[{earliest,latest,quantity}], locations[{department|aisle,bin}]
ikea --json stock 30449908 --all
ikea --json delivery 30449908:2 --postal-code "M5V 3L9" --store 149   # home_delivery.price/lead_time_days, click_and_collect, items[]
ikea --json delivery                            # uses the cart contents
```

## Cart

When logged in, cart commands change the user's **real** IKEA cart (same as ikea.com and the app). Confirm with the user before adding, changing or removing items they did not ask for. Not logged in, or with `--guest`, a guest cart stored on this machine is used (`data.guest: true`).

```bash
ikea --json cart                                # items[], total (before tax in Canada), currency, discount_codes
ikea --json cart add 30449908:2 10623220
ikea --json cart add --file items.txt           # "item quantity" per line, or JSON [{"item_no","quantity"}]; "-" = stdin
ikea --json cart set 30449908:4                 # exact quantity; 0 removes; item must already be in the cart
ikea --json cart remove 30449908
ikea --json cart coupon add CODE
ikea --json cart clear --yes                    # only when the user clearly asked to empty the cart
```

## Favourites lists

```bash
ikea --json lists                               # [{list_id, name, selected, items[]}]
ikea --json lists show "Living room"            # name or list_id; omit for the selected list
ikea --json lists create "Living room"
ikea --json lists add 30449908:2 --list "Living room"
ikea --json lists set 30449908:1 --list "Living room"
ikea --json lists remove 30449908 --list "Living room"
ikea --json lists rename "Living room" "Den"
ikea --json lists delete "Den" --yes            # only when asked
```

## Account (login required, read-only)

```bash
ikea --json orders -n 10          # purchases[]: id, type (order|receipt), status, date, total
ikea --json orders show NUMBER    # items, costs, deliveries
ikea --json orders status NUMBER  # deliveries[].status, timeline, estimated_from/to
ikea --json loyalty               # member, points, preferred_store
ikea --json rewards               # offers[] (points needed), claimed[]
ikea --json profile
```

## Login

Login needs the user, because IKEA's login form only works in a real browser. Never ask for their password.

1. `ikea --json auth login --start` returns `data.instructions` and `data.url`. Show the instructions to the user: sign in on ikea.com, open `view-source:` + the URL in the same browser, copy the whole page.
2. Pass what they paste to `ikea --json auth login --finish -` on stdin.
3. On a server, the user can instead give the `auth0` cookie: `ikea --json auth import --cookie -` (stdin).

Sessions refresh automatically. `session_expired` (also returned when IKEA answers 401) means they must log in again. `ikea auth logout` keeps the guest cart and lists; `--forget-guest` drops them.

## Limits

- No checkout, payment, order changes or point redemption: send the user to ikea.com for those.
- Prices in Canada are before tax. Delivery prices are estimates until checkout.
- Keep request volume modest; don't loop over hundreds of items.
- Never echo tokens, cookies or pasted login pages back to the user.
