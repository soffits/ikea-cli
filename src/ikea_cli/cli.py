"""The `ikea` command."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import traceback
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__, auth, markets, normalize
from .client import SORT_OPTIONS, IkeaClient
from .config import SETTABLE_KEYS, Paths, Settings, load_settings, normalize_postal_code, read_config_file, save_config
from .errors import EXIT_USAGE, IkeaError, LoginError, MarketNotSet, UsageError
from .output import dump_json, envelope_error, envelope_ok, kv, money, table

MAX_STDIN = 1_000_000


# =========================================================================== parsing helpers


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - exercised through main()
        raise UsageError(message, hint=f"Run `{self.prog} --help` for usage.")


def parse_item_spec(spec: str) -> tuple[str, int]:
    """'30449908', '304.499.08:2', '30449908x3' or '30449908*3' -> (item, quantity)."""

    raw = spec.strip()
    qty = 1
    for sep in (":", "x", "*", "="):
        if sep in raw and not raw.startswith("http"):
            head, _, tail = raw.rpartition(sep)
            if tail.strip().isdigit() and head:
                raw, qty = head.strip(), int(tail)
                break
    normalize.parse_item_ref(raw)  # validates; the API layer parses it again
    return raw, qty


def read_items_file(path: str) -> list[tuple[str, int]]:
    """Read items from a file ('-' for stdin): JSON list, or lines of 'item [qty]' / 'item,qty'."""

    text = _read_text(path)
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise UsageError(f"Items file is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise UsageError("A JSON items file must contain a list.")
        out = []
        for n, entry in enumerate(data, 1):
            try:
                if isinstance(entry, dict):
                    ref = entry.get("item_no") or entry.get("itemNo") or entry.get("item")
                    if not ref:
                        raise ValueError("no item number")
                    qty = entry.get("quantity", 1)
                    if isinstance(qty, bool) or not isinstance(qty, (int, str)) or not str(qty).strip().isdigit():
                        raise ValueError("bad quantity")
                    normalize.parse_item_ref(str(ref))
                    out.append((str(ref), int(qty)))
                elif isinstance(entry, (str, int)):
                    out.append(parse_item_spec(str(entry)))
                else:
                    raise ValueError("unsupported entry")
            except (ValueError, TypeError, UsageError) as exc:
                raise UsageError(
                    f"Entry {n} of the items file is not valid: {json.dumps(entry)[:80]}",
                    hint='Use entries like {"item_no": "30449908", "quantity": 2} or "304.499.08:2".',
                ) from exc
        return out
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in line.replace(",", " ").replace(";", " ").replace("\t", " ").split(" ") if p]
        try:
            if len(parts) == 1:
                out.append(parse_item_spec(parts[0]))
            else:
                out.append((parts[0], int(parts[1])))
        except (ValueError, UsageError) as exc:
            raise UsageError(f"Line {lineno} of the items file is not 'item [quantity]': {line!r}") from exc
    return out


def _read_text(path: str) -> str:
    if path == "-":
        data = sys.stdin.read(MAX_STDIN + 1)
    else:
        try:
            data = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"Cannot read {path}: {exc.strerror}") from exc
    if len(data) > MAX_STDIN:
        raise UsageError("Input is too large.")
    return data


def parse_filters(values: Sequence[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in values or []:
        if "=" not in v:
            out[v] = True
            continue
        key, _, val = v.partition("=")
        key = key.strip()
        if not key:
            raise UsageError(f"Bad filter {v!r}; use name=value, e.g. colors=10156.")
        existing = out.get(key)
        vals = [x.strip() for x in val.split(",") if x.strip()]
        out[key] = (existing if isinstance(existing, list) else []) + vals if vals else True
    return out


# =========================================================================== parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print machine-readable JSON")
    common.add_argument(
        "--country", default=argparse.SUPPRESS, help="IKEA market, e.g. us, gb, de, jp (see `ikea markets`; default from config)"
    )
    common.add_argument(
        "--language", default=argparse.SUPPRESS, help="language code (default from config, else the market's main language)"
    )
    common.add_argument("--guest", action="store_true", default=argparse.SUPPRESS, help="use a guest cart/list even when logged in")

    p = Parser(
        prog="ikea",
        description="Unofficial command-line client for the IKEA website (not affiliated with IKEA).",
        parents=[common],
        epilog="Run `ikea COMMAND --help` for details. Docs: https://github.com/soffits/ikea-cli",
    )
    p.add_argument("--version", action="version", version=f"ikea-cli {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)

    def cmd(parent: Any, name: str, help_: str, **kw: Any) -> argparse.ArgumentParser:
        return parent.add_parser(name, help=help_, description=help_, parents=[common], **kw)

    # auth
    a = cmd(sub, "auth", "log in, check or end your IKEA session")
    asub = a.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    lg = cmd(asub, "login", "log in with your browser (copy-paste, no password typed here)")
    lg.add_argument("--redirect", action="store_true", help="use the ikea.com redirect address instead of the code page")
    lg.add_argument("--start", action="store_true", help="only print the login link and remember the PKCE secret")
    lg.add_argument("--finish", metavar="FILE", help="finish a started login with pasted text from FILE ('-' = stdin)")
    lg.add_argument("--open", action="store_true", help="open the link in the default browser")
    im = cmd(asub, "import", "import an existing session (for servers without a browser)")
    src = im.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--cookie",
        metavar="FILE",
        help="file ('-' = stdin) with the `auth0` cookie from your market's accounts site (<country>.accounts.ikea.com)",
    )
    src.add_argument("--access-token", metavar="FILE", help="file ('-' = stdin) with an access token (works 24 h, no refresh)")
    cmd(asub, "status", "show who is logged in and when the session expires")
    cmd(asub, "refresh", "refresh the access token now")
    lo = cmd(asub, "logout", "revoke the refresh token and delete the local session")
    lo.add_argument("--local-only", action="store_true", help="only delete local files, do not contact IKEA")
    lo.add_argument("--forget-guest", action="store_true", help="also forget the guest session (its cart and lists become unreachable)")

    # markets
    mk = cmd(sub, "markets", "list the IKEA countries and languages ikea-cli works with")
    mk.add_argument("query", nargs="?", help="only markets whose code or name contains this text")
    mk.add_argument("--all", action="store_true", help="also list countries whose IKEA website is separate and not supported")

    # config
    c = cmd(sub, "config", "show or change settings")
    csub = c.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    cmd(csub, "show", "show the effective settings")
    cs = cmd(csub, "set", "set a value: " + ", ".join(SETTABLE_KEYS))
    cs.add_argument("key", choices=SETTABLE_KEYS)
    cs.add_argument("value")
    cu = cmd(csub, "unset", "remove a value")
    cu.add_argument("key", choices=SETTABLE_KEYS)
    cmd(csub, "path", "print where settings and the session are stored")

    # search
    s = cmd(sub, "search", "search products")
    s.add_argument("query", nargs="?", help="search text")
    s.add_argument("--category", "-c", help="category id to browse (see `ikea categories`)")
    s.add_argument(
        "--filter",
        "-f",
        action="append",
        metavar="NAME=ID[,ID]",
        help="filter, e.g. colors=10156 or price-buckets=PRICE_0_5000; repeatable",
    )
    s.add_argument("--sort", "-s", help=f"sort order: {', '.join(SORT_OPTIONS)}, or an IKEA sort id from --filters")
    s.add_argument("--limit", "-n", type=int, default=24, help="results per page (1-100, default 24)")
    s.add_argument("--page", "-p", type=int, default=1, help="page number (default 1)")
    s.add_argument("--offset", type=int, help="start at this result (overrides --page)")
    s.add_argument("--store", help="store id for availability filters (default from config)")
    s.add_argument("--postal-code", help="postal code context (default from config)")
    s.add_argument("--filters", dest="show_filters", action="store_true", help="list the filters you can use for this search")

    sg = cmd(sub, "suggest", "search suggestions for a prefix")
    sg.add_argument("prefix")

    ct = cmd(sub, "categories", "list product categories")
    ct.add_argument("query", nargs="?", help="only categories whose name contains this text")

    pr = cmd(sub, "product", "product details by item number or product URL")
    pr.add_argument("item")
    pr.add_argument("--quick", action="store_true", help="only the summary (one small request)")

    # stores and stock
    st = cmd(sub, "stores", "list IKEA stores in the market")
    st.add_argument("query", nargs="?", help="filter by name or city")
    st.add_argument("--refresh", action="store_true", help="reload the store list (cached for a day)")
    sto = cmd(sub, "store", "show or set your default store")
    stosub = sto.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    cmd(stosub, "show", "show the default store")
    ss = cmd(stosub, "set", "set the default store")
    ss.add_argument("store", help="store id or a unique part of its name")
    cmd(stosub, "unset", "forget the default store")

    sk = cmd(sub, "stock", "stock and restock dates per store")
    sk.add_argument("items", nargs="+", help="item numbers or product URLs")
    g = sk.add_mutually_exclusive_group()
    g.add_argument("--store", help="only this store (id or name); default: your default store, else all")
    g.add_argument("--all", action="store_true", help="all stores, even if a default store is set")

    dv = cmd(sub, "delivery", "delivery and click & collect options for a postal code")
    dv.add_argument("items", nargs="*", help="ITEM[:QTY] ...; omit to use your cart")
    dv.add_argument("--postal-code", help="postal code (default from config)")
    dv.add_argument("--store", help="store for click & collect / in-store (default from config)")

    # cart
    ca = cmd(sub, "cart", "your shopping cart (logged in: your real IKEA cart)")
    casub = ca.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    cmd(casub, "show", "show the cart with totals")
    cad = cmd(casub, "add", "add items")
    cad.add_argument("items", nargs="*", help="ITEM[:QTY] ...")
    cad.add_argument("--file", help="read 'item quantity' lines or a JSON list from FILE ('-' = stdin)")
    cse = cmd(casub, "set", "set the quantity of items already in the cart (0 removes)")
    cse.add_argument("items", nargs="+", help="ITEM:QTY ... (or ITEM QTY for a single item)")
    crm = cmd(casub, "remove", "remove items")
    crm.add_argument("items", nargs="+")
    ccl = cmd(casub, "clear", "remove everything from the cart")
    ccl.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    cco = cmd(casub, "coupon", "add or remove a discount code")
    cco.add_argument("op", choices=["add", "remove"])
    cco.add_argument("code")

    # favourites
    li = cmd(sub, "lists", "favourites lists (logged in: your account's lists)")
    lisub = li.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    lsh = cmd(lisub, "show", "show a list's items (default: the selected list)")
    lsh.add_argument("list", nargs="?")
    lcr = cmd(lisub, "create", "create a list")
    lcr.add_argument("name")
    lrn = cmd(lisub, "rename", "rename a list")
    lrn.add_argument("list")
    lrn.add_argument("name")
    ldl = cmd(lisub, "delete", "delete a list")
    ldl.add_argument("list")
    ldl.add_argument("--yes", "-y", action="store_true")
    lse = cmd(lisub, "select", "make a list the default one")
    lse.add_argument("list")
    for name, help_ in (("add", "add items to a list"), ("set", "set quantities in a list")):
        x = cmd(lisub, name, help_)
        x.add_argument("items", nargs="+", help="ITEM[:QTY] ...")
        x.add_argument("--list", "-l", help="list id or name (default: the selected list)")
    lrm = cmd(lisub, "remove", "remove items from a list")
    lrm.add_argument("items", nargs="+")
    lrm.add_argument("--list", "-l")

    # orders
    o = cmd(sub, "orders", "your purchases (online orders and store receipts)")
    o.add_argument("--limit", "-n", type=int, default=argparse.SUPPRESS, help="purchases to show (default 20)")
    o.add_argument("--offset", type=int, default=argparse.SUPPRESS, help="skip this many purchases")
    osub = o.add_subparsers(dest="action", metavar="ACTION", parser_class=Parser)
    ol = cmd(osub, "list", "list purchases (default)")
    ol.add_argument("--limit", "-n", type=int, default=argparse.SUPPRESS, help="purchases to show (default 20)")
    ol.add_argument("--offset", type=int, default=argparse.SUPPRESS, help="skip this many purchases")
    os_ = cmd(osub, "show", "details of one purchase")
    os_.add_argument("number")
    ot = cmd(osub, "status", "delivery status of an order")
    ot.add_argument("number")

    cmd(sub, "loyalty", "IKEA Family membership and points")
    cmd(sub, "rewards", "rewards you can redeem and rewards you claimed")
    cmd(sub, "profile", "your IKEA account profile")
    return p


# =========================================================================== command handlers

Handler = Callable[[argparse.Namespace, "Context"], tuple[Any, str | None]]


class Context:
    def __init__(self, args: argparse.Namespace, paths: Paths) -> None:
        self.args = args
        self.paths = paths
        overrides = {"country": getattr(args, "country", None), "language": getattr(args, "language", None)}
        self.settings = load_settings(paths, overrides)
        self._client: IkeaClient | None = None

    @property
    def client(self) -> IkeaClient:
        if self._client is None:
            self._client = IkeaClient(self.settings, paths=self.paths, guest=bool(getattr(self.args, "guest", False)))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @property
    def interactive(self) -> bool:
        return sys.stdin.isatty() and not getattr(self.args, "json", False)

    def money(self, value: Any, currency: str | None = None) -> str:
        return self.client.format_price(value, currency) if value is not None else "-"


def confirm(ctx: Context, question: str, yes: bool) -> None:
    if yes:
        return
    if not ctx.interactive:
        raise UsageError(f"{question} Pass --yes to confirm.")
    answer = input(f"{question} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise UsageError("Cancelled.")


# --------------------------------------------------------------------------- auth


LOGIN_STEPS_PAGE = """\
How to log in (your password is only ever typed on ikea.com):

  1. In your normal browser, sign in at
       https://www.ikea.com/{cc}/{lang}/profile/login/
     and tick "Keep me signed in". Skip this if you are already signed in.

  2. In the same browser, open this link with "view-source:" in front of it
     (paste the whole line into the address bar):

       view-source:{url}

     Safari: open the link normally, then use Develop > Show Page Source.

  3. Select everything on that page (Ctrl+A / Cmd+A), copy it, and paste it here.
     The page is about 2 KB of text and contains the one-time code.
"""

LOGIN_STEPS_REDIRECT = """\
How to log in (your password is only ever typed on ikea.com):

  1. Open this link in your browser and sign in:

       {url}

  2. You will land on ikea.com. Right away, copy the full address from the address bar
     (it contains "#code="). If the page already moved on, find the address that starts with
     https://www.ikea.com/{cc}/{lang}/profile/login/#code= in your browser history.

  3. Paste the address here.
"""


def _start_login(ctx: Context, mode: str) -> dict[str, Any]:
    verifier, challenge = auth.make_pkce()
    state = secrets.token_urlsafe(24)
    url = auth.authorize_url(ctx.settings, challenge, state, mode=mode)
    store = auth.SessionStore(ctx.paths)
    store.save_pending({"verifier": verifier, "state": state, "mode": mode, "country": ctx.settings.country, "created_at": time.time()})
    template = LOGIN_STEPS_PAGE if mode == "page" else LOGIN_STEPS_REDIRECT
    return {"url": url, "mode": mode, "instructions": template.format(url=url, cc=ctx.settings.country, lang=ctx.settings.language)}


def _finish_login(ctx: Context, pasted: str) -> dict[str, Any]:
    store = auth.SessionStore(ctx.paths)
    pending = store.load_pending()
    if not pending or pending.get("country") != ctx.settings.country:
        raise LoginError("No login is in progress (or it is older than 15 minutes).", hint="Run `ikea auth login` again.")
    resp = auth.parse_auth_response(pasted)
    if resp.error or not resp.code:
        raise auth.explain_auth_error(resp)
    if resp.state and resp.state != pending["state"]:
        raise LoginError(
            "The pasted code belongs to a different login attempt.", hint="Open the most recent link printed by `ikea auth login`."
        )
    tokens = auth.exchange_code(ctx.client.transport, ctx.settings, resp.code, pending["verifier"])
    session = ctx.client.auth.complete_login(tokens, "login")
    store.clear_pending()
    return session.summary()


def _read_paste() -> str:
    print("\nPaste here, then press Enter on an empty line (or Ctrl+D):", file=sys.stderr)
    lines: list[str] = []
    total = 0
    for line in sys.stdin:
        total += len(line)
        if total > MAX_STDIN:
            raise UsageError("Pasted text is too large.")
        if not line.strip() and lines:
            joined = "".join(lines)
            if auth.parse_auth_response(joined).code or "authorizationResponse" in joined or "code=" in joined:
                break
        lines.append(line)
        joined = "".join(lines)
        if "</html>" in joined.lower():
            break
        if len(lines) == 1 and ("code=" in line or auth.parse_auth_response(line).code):
            break
    return "".join(lines)


def h_auth(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action
    a = ctx.client.auth
    if action == "login":
        if args.finish:
            summary = _finish_login(ctx, _read_text(args.finish))
            return summary, f"Logged in{_who(summary)}. Session saved."
        mode = "redirect" if args.redirect else "page"
        started = _start_login(ctx, mode)
        if args.open:
            webbrowser.open(started["url"])
        if args.start or getattr(args, "json", False) or not sys.stdin.isatty():
            hint = "Then run `ikea auth login --finish -` and paste the page or address."
            return {**started, "next": "ikea auth login --finish -"}, started["instructions"] + "\n" + hint
        print(started["instructions"], file=sys.stderr)
        summary = _finish_login(ctx, _read_paste())
        return summary, f"Logged in{_who(summary)}. Session saved."
    if action == "import":
        if args.access_token:
            token = _read_text(args.access_token).strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            claims = auth.jwt_claims(token)
            if not claims.get("exp"):
                raise LoginError("That does not look like an IKEA access token.")
            session = a.complete_login({"access_token": token}, "access-token")  # checks expiry before saving
            summary = session.summary()
            return summary, f"Imported access token{_who(summary)}. It cannot be refreshed; it expires {summary['expires_at']}."
        cookie = _read_text(args.cookie).strip()
        verifier, challenge = auth.make_pkce()
        state = secrets.token_urlsafe(24)
        resp = auth.code_from_cookie(ctx.client.transport, ctx.settings, cookie, challenge, state)
        if resp.error or not resp.code:
            if resp.error == "login_required":
                raise LoginError("IKEA did not accept that cookie (the browser session has ended or the value is incomplete).")
            raise auth.explain_auth_error(resp)
        tokens = auth.exchange_code(ctx.client.transport, ctx.settings, resp.code, verifier)
        summary = a.complete_login(tokens, "cookie").summary()
        return summary, f"Logged in{_who(summary)} from the browser session. The cookie itself was not stored."
    if action == "status":
        s = a.session()
        if s is None:
            return {"logged_in": False, "country": ctx.settings.country}, "Not logged in. Cart and lists use a guest session."
        summary = s.summary()
        if s.is_expired() and not s.refresh_token:
            summary["logged_in"] = False
            return summary, "Your session has expired. Run `ikea auth login`."
        left = summary["expires_in_seconds"]
        text = kv(
            [
                ("Logged in as", summary.get("name") or summary.get("email") or "IKEA customer"),
                ("Email", summary.get("email")),
                ("Market", f"{ctx.client.market.name} ({ctx.client.cc})"),
                ("Loyalty", ", ".join(summary.get("loyalty_programs") or []) or None),
                ("Access token", f"valid for {left // 3600} h {left % 3600 // 60} min" if left else "expired (will refresh)"),
                ("Auto-refresh", summary["can_refresh"]),
                ("Source", summary["source"]),
            ]
        )
        return summary, text
    if action == "refresh":
        summary = a.refresh(force=True).summary()
        return summary, f"Session refreshed; valid until {summary['expires_at']}."
    if action == "logout":
        result = a.logout(revoke=not args.local_only, forget_guest=args.forget_guest)
        msg = "Logged out." if result["removed_local_session"] else "No local session was stored."
        if result["removed_guest_session"]:
            msg += " The guest cart and lists on this computer were forgotten too."
        if result.get("revoke_error"):
            msg += f" (Could not revoke the token at IKEA: {result['revoke_error']})"
        return result, msg
    raise UsageError("Choose an action: login, import, status, refresh, logout.")


def _who(summary: dict[str, Any]) -> str:
    name = summary.get("name") or summary.get("email")
    return f" as {name}" if name else ""


# --------------------------------------------------------------------------- config


def h_config(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action or "show"
    if action == "path":
        data = {
            "config_file": str(ctx.paths.config_file),
            "state_dir": str(ctx.paths.state_dir),
            "cache_dir": str(ctx.paths.cache_dir),
        }
        return data, kv(list(data.items()))
    if action == "show":
        data = ctx.settings.to_dict()
        data["verified_market"] = ctx.settings.verified
        market = markets.get(ctx.settings.country)
        data["market_name"] = market.name if market else None
        data["currency"] = market.currency if market else None
        text = kv([(k, v) for k, v in data.items()])
        if not ctx.settings.country:
            text = (text + "\n\n" if text else "") + "No market set. Pick one from `ikea markets`, then: ikea config set country CODE"
        return data, text
    current = read_config_file(ctx.paths)
    if action == "set":
        value: Any = args.value
        notes: list[str] = []
        trial = dict(current)
        if args.key == "country":
            market, language = markets.resolve(value, current.get("language") if _offers(value, current.get("language")) else None)
            value = market.country
            if current.get("country") and current["country"] != value:
                for key in ("store", "postal_code"):
                    if trial.pop(key, None) is not None:
                        notes.append(f"Removed {key} (it belonged to the previous market).")
            if current.get("language") and current["language"] != language:
                notes.append(
                    f"Language set to {language} ({market.languages[language]}); IKEA {market.name} offers {', '.join(market.languages)}."
                )
            trial["language"] = language
        else:
            if not ctx.settings.country:
                raise MarketNotSet("Choose your IKEA market before other settings.")
            if args.key == "language":
                value = markets.resolve(ctx.settings.country, value)[1]
            if args.key == "postal_code":
                value = normalize_postal_code(value)
            if args.key == "store":
                value = ctx.client.find_store(value)["id"]
        trial[args.key] = value
        if ctx.settings.implicit_country and "country" not in trial:
            trial["country"] = ctx.settings.country
        Settings(**{k: v for k, v in trial.items() if k in SETTABLE_KEYS})  # validate
        save_config(ctx.paths, trial)
        return trial, "\n".join([f"Set {args.key} = {value}", *notes])
    if action == "unset":
        current.pop(args.key, None)
        save_config(ctx.paths, current)
        return current, f"Removed {args.key}"
    raise UsageError("Choose an action: show, set, unset, path.")


def _offers(country: str, language: str | None) -> bool:
    market = markets.get(country)
    return bool(market and language and language in market.languages)


def _features(m: markets.Market) -> list[tuple[str, bool]]:
    return [
        ("login", bool(m.auth_domain)),
        ("cart", m.offers("cart")),
        ("lists", m.offers("lists")),
        ("delivery", m.offers("delivery")),
    ]


def h_markets(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    matches = markets.search(args.query)
    found = [m for m in matches if m.has_shop]
    data: list[dict[str, Any]] = [{**m.to_dict(), "supported": True} for m in found]
    rows = [
        [
            m.country + (" *" if m.country == ctx.settings.country else ""),
            m.name,
            ", ".join(m.languages),
            m.currency,
            ", ".join(f for f, ok in _features(m) if not ok) or "-",
            m.tested or "-",
        ]
        for m in found
    ]
    text = table(rows, ["Code", "Country", "Languages", "Currency", "Not available", "Tested"]) if rows else "No market matches."
    if args.all:
        q = (args.query or "").lower()
        others = [
            {"country": cc, "name": o.get("name"), "url": o.get("url"), "served_by": o.get("served_by"), "supported": False}
            for cc, o in sorted(markets.other_sites().items())
            if not q or q == cc or q in str(o.get("name") or "").lower()
        ]
        others += [
            {
                "country": m.country,
                "name": m.name,
                "url": f"https://www.ikea.com/{m.country}/{m.default_language}/",
                "served_by": None,
                "supported": False,
            }
            for m in matches
            if not m.has_shop
        ]
        data.extend(others)
        if others:
            text += "\n\nNot supported (separate IKEA websites, or no online range):\n" + table(
                [[o["country"], o["name"], f"use {o['served_by']}" if o.get("served_by") else o["url"]] for o in others],
                ["Code", "Country", "Website"],
            )
    elif rows:
        text += "\n\nSet yours with: ikea config set country CODE   (--all also lists unsupported countries)"
    if rows and ctx.settings.country:
        text += "\n* = your market"
    if markets.table_date():
        text += f"\nMarket list from ikea.com, {markets.table_date()}. Tested = guest features last checked live."
    return data, text


# --------------------------------------------------------------------------- catalogue


def h_search(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    if args.page < 1:
        raise UsageError("--page starts at 1.")
    offset = args.offset if args.offset is not None else (args.page - 1) * args.limit
    store = ctx.client.find_store(args.store)["id"] if args.store else None
    result = ctx.client.search(
        args.query,
        category=args.category,
        filters=parse_filters(args.filter),
        sort=args.sort,
        limit=args.limit,
        offset=offset,
        store=store,
        postal_code=args.postal_code,
    )
    if args.show_filters:
        lines = []
        for f in result["filters"]:
            if not f["values"]:
                continue
            lines.append(f"{f['name']}  (--filter {f['parameter'].removeprefix('f-')}=ID)")
            for v in f["values"][:25]:
                count = f" ({v['count']})" if v.get("count") is not None else ""
                mark = " *" if v.get("selected") else ""
                lines.append(f"    {v['id']:<28} {v['name']}{count}{mark}")
        aliases = {v: k for k, v in SORT_OPTIONS.items()}
        lines.append("")
        lines.append("Sort  (--sort ID or the short name)")
        for opt in result.get("sort_options") or []:
            short = aliases.get(opt["id"])
            lines.append(f"    {opt['id']:<34} {opt['name']}" + (f"  [{short}]" if short else ""))
        return result, "\n".join(lines)
    items = result["items"]
    if not items:
        hint = ""
        if result.get("did_you_mean"):
            hint = f" Did you mean: {', '.join(map(str, result['did_you_mean']))}?"
        return result, "No products found." + hint
    rows = [
        [
            normalize.format_item_no(i["item_no"] or ""),
            i["name"],
            ", ".join(x for x in (i["type_name"], i["description"], i["measure"]) if x),
            ctx.money(i["price"], i["currency"]),
            f"{i['rating']:.1f} ({i['rating_count']})" if i.get("rating") else "-",
        ]
        for i in items
    ]
    start = result["offset"] + 1
    footer = f"\n{start}-{start + len(items) - 1} of {result['total']}"
    if result["offset"] + len(items) < result["total"]:
        nxt = f"--page {args.page + 1}" if args.offset is None else f"--offset {result['offset'] + len(items)}"
        footer += f"  (next: {nxt})"
    footer += "   Filters: add --filters"
    return result, table(rows, ["Item", "Name", "Description", "Price", "Rating"], align_right=[3]) + footer


def h_suggest(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    words = ctx.client.suggest(args.prefix)
    return {"prefix": args.prefix, "suggestions": words}, "\n".join(words) or "No suggestions."


def h_categories(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    cats = ctx.client.categories(args.query)
    text = table([[c["id"], c["name"]] for c in cats], ["Id", "Category"]) if cats else "No categories found."
    return cats, text + ("\n\nBrowse one with: ikea search --category ID" if cats else "")


def h_product(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    p = ctx.client.product_summary(args.item) if args.quick else ctx.client.product(args.item)
    pairs: list[tuple[str, Any]] = [
        ("Item", f"{normalize.format_item_no(p['item_no'])}{' (combination)' if p.get('item_type') == 'SPR' else ''}"),
        ("Name", p.get("name")),
        ("Type", p.get("type_name") or p.get("description")),
        ("Description", p.get("description") if p.get("type_name") else None),
        ("Size", p.get("measure")),
        ("Price", ctx.money(p.get("price"), p.get("currency"))),
        ("Rating", f"{p['rating']:.1f} / 5 ({p.get('rating_count')} reviews)" if p.get("rating") else None),
        ("Online", None if p.get("online_sellable") is None else ("can be bought online" if p["online_sellable"] else "in store only")),
        ("Designer", p.get("designer")),
        ("URL", p.get("url")),
    ]
    parts = [kv(pairs)]
    if p.get("summary"):
        parts.append("\n".join(p["summary"]))
    if p.get("dimensions"):
        parts.append("Dimensions\n" + kv([(d["name"], d["value"]) for d in p["dimensions"]]))
    if p.get("packages"):
        rows = [
            [pk.get("name"), pk.get("item_no"), pk.get("quantity"), pk.get("width"), pk.get("height"), pk.get("length"), pk.get("weight")]
            for pk in p["packages"]
        ]
        parts.append(
            f"Packages ({p.get('number_of_packages')})\n" + table(rows, ["Package", "Item", "Qty", "Width", "Height", "Length", "Weight"])
        )
    if p.get("materials"):
        parts.append("Materials\n" + "\n".join(f"  {m}" for m in p["materials"]))
    for v in p.get("variants") or []:
        opts = ", ".join(f"{o['name']} [{o['item_no']}]{' *' if o['selected'] else ''}" for o in v["options"])
        parts.append(f"{v['title']}: {opts}")
    if p.get("images"):
        parts.append(f"Images: {len(p['images'])} (see --json)")
    return p, "\n\n".join(x for x in parts if x)


# --------------------------------------------------------------------------- stores, stock, delivery


def h_stores(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    stores = ctx.client.stores(refresh=args.refresh)
    if args.query:
        q = args.query.lower()
        stores = [s for s in stores if q in str(s["name"]).lower() or q in str(s.get("city") or "").lower() or q == str(s["id"])]
    default = ctx.settings.store
    rows = [[s["id"] + (" *" if s["id"] == default else ""), s["name"], s.get("city"), s.get("province"), s.get("kind")] for s in stores]
    note = "\n* = your default store" if default else "\nSet a default with: ikea store set ID"
    return stores, table(rows, ["Id", "Store", "City", "Region", "Type"]) + note


def h_store(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action or "show"
    if action == "set":
        s = ctx.client.find_store(args.store)
        current = read_config_file(ctx.paths)
        current["store"] = s["id"]
        save_config(ctx.paths, current)
        return s, f"Default store: {s['name']} ({s['id']})"
    if action == "unset":
        current = read_config_file(ctx.paths)
        current.pop("store", None)
        save_config(ctx.paths, current)
        return {"store": None}, "Default store removed."
    if not ctx.settings.store:
        return None, "No default store. Set one with: ikea store set ID"
    s = ctx.client.find_store(ctx.settings.store)
    hours = ", ".join(f"{h['day'].title()} {h['open']}-{h['close']}" for h in s.get("hours") or [])
    return s, kv(
        [
            ("Store", f"{s['name']} ({s['id']})"),
            ("Address", s.get("address")),
            ("City", s.get("city")),
            ("Hours", hours),
            ("URL", s.get("url")),
        ]
    )


def h_stock(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    store = None
    if not args.all:
        ref = args.store or ctx.settings.store
        store = ctx.client.find_store(ref)["id"] if ref else None
    result = ctx.client.stock(args.items, store=store)
    blocks = []
    for r in result:
        head = normalize.format_item_no(r["item_no"])
        if r.get("error"):
            blocks.append(f"{head}: {r['error']}")
            continue
        hd = r.get("home_delivery") or {}
        head += f"  home delivery: {hd.get('status') or ('available' if hd.get('available') else 'not available')}" if hd else ""
        rows = []
        for s in r["stores"]:
            restock = ", ".join(
                f"{x['quantity']} on {x['earliest']}" + (f"-{x['latest']}" if x.get("latest") and x["latest"] != x["earliest"] else "")
                for x in s["restocks"]
            )
            where = "; ".join(
                " ".join(
                    str(v)
                    for v in (loc.get("department"), loc.get("aisle") and f"aisle {loc['aisle']}", loc.get("bin") and f"bin {loc['bin']}")
                    if v
                )
                for loc in s.get("locations") or []
            )
            rows.append([s["store"], s["quantity"], s["status"], restock or "-", where or "-"])
        blocks.append(
            head + "\n" + (table(rows, ["Store", "Qty", "Status", "Restock", "Location"], align_right=[1]) if rows else "  (no store data)")
        )
    return result, "\n\n".join(blocks)


def h_delivery(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    store = ctx.client.find_store(args.store)["id"] if args.store else None
    items = [parse_item_spec(i) for i in args.items]
    result = ctx.client.delivery(items or None, postal_code=args.postal_code, store=store, from_cart=not items)
    hd, cc = result["home_delivery"], result["click_and_collect"]
    if hd["price"] is not None:
        home = ctx.money(hd["price"])
    elif any(i["home_delivery"] for i in result["items"]):
        home = "no price offered for these items alone (IKEA may need a larger order)"
    else:
        home = "not available"
    if cc["price"] is not None:
        collect = ctx.money(cc["price"])
    else:
        collect = "not available" if result.get("store_id") else "set a store to check"
    lines = [
        f"Postal code {result['postal_code']}"
        + (
            f" ({', '.join(x for x in (result.get('city'), result.get('province')) if x)})"
            if result.get("city") or result.get("province")
            else ""
        ),
        f"Home delivery:     {home}"
        + (f"  (was {ctx.money(hd['original_price'])})" if hd.get("original_price") and hd["original_price"] != hd["price"] else "")
        + (f", about {hd['lead_time_days']} days" if hd.get("lead_time_days") else ""),
        f"Click & collect:   {collect}" + (f", about {cc['lead_time_days']} days" if cc.get("lead_time_days") else ""),
    ]
    rows = [
        [
            normalize.format_item_no(i["item_no"] or ""),
            i["home_delivery"],
            i["click_and_collect"],
            "-" if i["in_store"] is None else f"{'yes' if i['in_store'] else 'no'} ({i['in_store_quantity']})",
            (i.get("in_store_restock") or {}).get("earliestDate") or "-",
        ]
        for i in result["items"]
    ]
    return result, "\n".join(lines) + "\n\n" + table(rows, ["Item", "Delivery", "Collect", "In store", "Restock"])


# --------------------------------------------------------------------------- cart


def render_cart(cart: dict[str, Any], price: Callable[[Any, str | None], str] = money) -> str:
    who = "Guest cart" if cart.get("guest") else "Your IKEA cart"
    if cart["empty"] or not cart["items"]:
        return f"{who} is empty."
    cur = cart.get("currency")
    rows = [
        [
            normalize.format_item_no(i["item_no"] or ""),
            i["name"],
            ", ".join(x for x in (i.get("type_name"), i.get("description"), i.get("measure")) if x),
            i["quantity"],
            price(i["unit_price"], cur),
            price(i["subtotal"], cur),
        ]
        for i in cart["items"]
    ]
    text = f"{who}: {cart['lines']} lines, {cart['quantity']} items\n\n"
    text += table(rows, ["Item", "Name", "Description", "Qty", "Each", "Subtotal"], align_right=[3, 4, 5])
    total_label = "Total (before tax)" if cart.get("total_incl_tax") is None else "Total"
    text += f"\n\n{total_label}: {price(cart['total'], cur)}"
    if cart.get("savings"):
        text += f"   You save: {price(cart['savings'], cur)}"
    if cart.get("discount_codes"):
        text += f"\nDiscount codes: {', '.join(map(str, cart['discount_codes']))}"
    return text


def h_cart(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action or "show"
    cl = ctx.client
    if action == "show":
        cart = cl.cart()
        return cart, render_cart(cart, ctx.money)
    if action == "add":
        items = [parse_item_spec(i) for i in args.items]
        if args.file:
            items += read_items_file(args.file)
        if not items:
            raise UsageError("Give items to add, e.g. `ikea cart add 304.499.08:2`, or --file.")
        cart = cl.cart_add(items)
        return cart, f"Added {sum(q for _, q in items)} item(s).\n\n" + render_cart(cart, ctx.money)
    if action == "set":
        if len(args.items) == 2 and args.items[1].isdigit() and len(args.items[1]) <= 3 and ":" not in args.items[0]:
            items = [(args.items[0], int(args.items[1]))]
        else:
            items = []
            for spec in args.items:
                if not any(sep in spec for sep in (":", "=")):
                    raise UsageError(f"Give the quantity as ITEM:QTY ({spec!r}).")
                items.append(parse_item_spec(spec))
        cart = cl.cart_set(items)
        return cart, "Updated.\n\n" + render_cart(cart, ctx.money)
    if action == "remove":
        cart = cl.cart_remove(args.items)
        return cart, "Removed.\n\n" + render_cart(cart, ctx.money)
    if action == "clear":
        confirm(ctx, "Remove everything from the cart?", args.yes)
        cart = cl.cart_clear()
        return cart, "Cart cleared."
    if action == "coupon":
        cart = cl.cart_add_coupon(args.code) if args.op == "add" else cl.cart_remove_coupon(args.code)
        return cart, ("Discount code added." if args.op == "add" else "Discount code removed.") + "\n\n" + render_cart(cart, ctx.money)
    raise UsageError("Choose an action: show, add, set, remove, clear, coupon.")


# --------------------------------------------------------------------------- favourites


def render_list(lst: dict[str, Any], price: Callable[[Any, str | None], str] = money) -> str:
    title = f"{lst['name'] or '(unnamed list)'}{' [selected]' if lst['selected'] else ''}  id {lst['list_id']}"
    if not lst["items"]:
        return title + "\n(empty)"
    rows = [
        [normalize.format_item_no(i["item_no"] or ""), i["name"], i.get("description"), i["quantity"], price(i.get("unit_price"), None)]
        for i in lst["items"]
    ]
    return title + "\n" + table(rows, ["Item", "Name", "Description", "Qty", "Price"], align_right=[3, 4])


def h_lists(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action
    cl = ctx.client
    if action is None:
        lists = cl.lists()
        if not lists:
            return lists, "No favourites lists yet. Create one with: ikea lists create NAME"
        rows = [[x["list_id"], x["name"] or "(unnamed)", len(x["items"]), "yes" if x["selected"] else "", x["updated"]] for x in lists]
        return lists, table(rows, ["Id", "Name", "Items", "Selected", "Updated"])
    if action == "show":
        lst = cl.get_list(args.list)
        return lst, render_list(lst, ctx.money)
    if action == "create":
        lst = cl.list_create(args.name)
        return lst, f"Created list {lst['name']!r} ({lst['list_id']})."
    if action == "rename":
        lst = cl.list_rename(args.list, args.name)
        return lst, f"Renamed to {lst['name']!r}."
    if action == "delete":
        confirm(ctx, f"Delete the list {args.list!r}?", args.yes)
        remaining = cl.list_delete(args.list)
        return remaining, "List deleted."
    if action == "select":
        lists = cl.list_select(args.list)
        return lists, "Selected."
    if action in ("add", "set"):
        items = [parse_item_spec(i) for i in args.items]
        lst = cl.list_add(items, list_ref=args.list) if action == "add" else cl.list_set(items, list_ref=args.list)
        return lst, render_list(lst, ctx.money)
    if action == "remove":
        lst = cl.list_remove(args.items, list_ref=args.list)
        return lst, render_list(lst, ctx.money)
    raise UsageError("Unknown lists action.")


# --------------------------------------------------------------------------- orders and loyalty


def h_orders(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    action = args.action or "list"
    cl = ctx.client
    if action == "list":
        limit = getattr(args, "limit", 20)
        offset = getattr(args, "offset", 0)
        if not 1 <= limit <= 100:
            raise UsageError("--limit must be between 1 and 100.")
        data = cl.orders(limit=limit, offset=offset)
        if not data["purchases"]:
            return data, "No purchases found."
        rows = [
            [p["id"], (p["date"] or "")[:10], p["type"], p["status"], p.get("store"), p.get("total_formatted"), p.get("item_count")]
            for p in data["purchases"]
        ]
        return data, table(
            rows, ["Number", "Date", "Type", "Status", "Store", "Total", "Items"], align_right=[5, 6]
        ) + f"\n\n{data['total']} purchases in total"
    if action == "show":
        d = cl.order(args.number)
        costs = d.get("costs") or {}
        parts = [
            kv(
                [
                    ("Number", d["id"]),
                    ("Type", d["type"]),
                    ("Status", d["status"]),
                    ("Date", d["date"]),
                    ("Store", d.get("store")),
                    *[(k.title(), v.get("formatted")) for k, v in costs.items() if v],
                ]
            )
        ]
        if d["items"]:
            rows = [
                [normalize.format_item_no(i["item_no"] or ""), i["name"], i.get("description"), i["quantity"], i.get("total_price")]
                for i in d["items"]
            ]
            parts.append(table(rows, ["Item", "Name", "Description", "Qty", "Total"], align_right=[3, 4]))
        parts.extend(_render_deliveries(d.get("deliveries") or []))
        return d, "\n\n".join(parts)
    if action == "status":
        d = cl.order_status(args.number)
        return d, "\n\n".join([f"Order {d['id']}: {d['status']}", *_render_deliveries(d["deliveries"])])
    raise UsageError("Choose an action: list, show, status.")


def _render_deliveries(deliveries: list[dict[str, Any]]) -> list[str]:
    out = []
    for dl in deliveries:
        when = dl.get("delivered_at") or (
            f"{dl.get('estimated_from') or '?'} to {dl.get('estimated_to') or '?'}" if dl.get("estimated_from") else dl.get("window")
        )
        steps = " > ".join(f"{s['status']}{' (now)' if s['tense'] == 'PRESENT' else ''}" for s in dl.get("timeline") or [])
        out.append(kv([("Delivery", dl.get("type")), ("Status", dl.get("status")), ("When", when), ("Progress", steps)]))
    return out


def h_loyalty(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    d = ctx.client.loyalty()
    store = d.get("preferred_store") or {}
    text = kv(
        [
            ("Name", d.get("name")),
            ("IKEA Family", "active member" if d["member"] else "not a member"),
            ("Programs", ", ".join(f"{m['program']} ({m['status']}, since {m['member_since']})" for m in d["memberships"])),
            ("Card", d.get("card_number")),
            ("Points", d.get("points")),
            ("Preferred store", f"{store.get('name') or ''} ({store.get('id')})" if store else None),
        ]
    )
    return d, text


def h_rewards(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    d = ctx.client.rewards()
    rows = [[o["points"], o["name"], o["category"], "yes" if (d.get("points") or 0) >= (o["points"] or 0) else ""] for o in d["offers"]]
    text = f"Points: {d.get('points')}\n\n" + table(rows, ["Points", "Reward", "Kind", "Affordable"], align_right=[0])
    if d["claimed"]:
        lines = [
            f"  {c.get('name') or '(unnamed reward)'} ({c.get('status') or '?'}, valid to {c.get('valid_to') or '?'})" for c in d["claimed"]
        ]
        text += "\n\nClaimed:\n" + "\n".join(lines)
    else:
        text += "\n\nNo claimed rewards."
    text += "\nRedeem rewards on ikea.com; this tool does not spend points."
    return d, text


def h_profile(args: argparse.Namespace, ctx: Context) -> tuple[Any, str | None]:
    d = ctx.client.profile()
    return d, kv([(k.replace("_", " ").capitalize(), v) for k, v in d.items()])


HANDLERS: dict[str, Handler] = {
    "auth": h_auth,
    "config": h_config,
    "markets": h_markets,
    "search": h_search,
    "suggest": h_suggest,
    "categories": h_categories,
    "product": h_product,
    "stores": h_stores,
    "store": h_store,
    "stock": h_stock,
    "delivery": h_delivery,
    "cart": h_cart,
    "lists": h_lists,
    "orders": h_orders,
    "loyalty": h_loyalty,
    "rewards": h_rewards,
    "profile": h_profile,
}


# =========================================================================== main

MARKET_FREE = ("config", "markets")


def _save_implicit_country(ctx: Context) -> None:
    """Earlier versions always used Canada without storing it; write it down once so it stays explicit."""

    current = read_config_file(ctx.paths)
    current.setdefault("country", ctx.settings.country)
    save_config(ctx.paths, current)
    print(
        f"note: ikea-cli now supports many countries. Your settings had none, so '{ctx.settings.country}' was saved "
        "(as before). Change it with `ikea config set country CODE`.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    parser = build_parser()
    ctx: Context | None = None
    try:
        args = parser.parse_args(argv)
        want_json = bool(getattr(args, "json", False))
        if not args.command:
            parser.print_help(sys.stderr)
            return EXIT_USAGE
        ctx = Context(args, Paths.default())
        if args.command not in MARKET_FREE:
            ctx.settings.market()  # clear error if no market is set or it can't be served
            if ctx.settings.implicit_country:
                _save_implicit_country(ctx)
            if not ctx.settings.verified and not want_json:
                print(
                    f"note: ikea-cli has not been tested in {ctx.settings.country}/{ctx.settings.language} yet; "
                    "please report anything that does not work.",
                    file=sys.stderr,
                )
        data, text = HANDLERS[args.command](args, ctx)
        if want_json:
            dump_json(envelope_ok(data))
        elif text is not None:
            print(text)
        return 0
    except IkeaError as exc:
        return _report(exc, want_json)
    except KeyboardInterrupt:
        return _report(UsageError("Interrupted."), want_json)
    except BrokenPipeError:
        # Output was piped into something like `head`; exit quietly.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except Exception as exc:
        # Last resort, e.g. IKEA changed a response shape. Never prints request data, only the error.
        if os.environ.get("IKEA_DEBUG") == "1":
            traceback.print_exc()
        detail = " ".join(str(exc).split())[:200]
        error = IkeaError(
            f"Unexpected error ({exc.__class__.__name__}){': ' + detail if detail else ''}.",
            hint="Run again with IKEA_DEBUG=1 for details. If it keeps happening, IKEA may have changed something; please report it.",
        )
        return _report(error, want_json)
    finally:
        if ctx is not None:
            ctx.close()


def _report(exc: IkeaError, want_json: bool) -> int:
    if want_json:
        dump_json(envelope_error(exc.to_dict()), sys.stdout)
    else:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
    return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["build_parser", "main", "parse_filters", "parse_item_spec", "read_items_file"]
