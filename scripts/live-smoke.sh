#!/usr/bin/env bash
# Run every ikea command once against the real IKEA website, as a guest, in one market.
# Account commands run read-only only if IKEA_ACCESS_TOKEN is set; nothing in the account is changed.
#
#   scripts/live-smoke.sh                 # Canada, English
#   scripts/live-smoke.sh de/de           # any market from `ikea markets`
#   IKEA_ACCESS_TOKEN=... scripts/live-smoke.sh us/en
#
# Products, a store and a postal code are picked from IKEA's own data for that market
# (a search, the store list, and that store's postal code). Override with IKEA_SMOKE_STORE
# and IKEA_SMOKE_POSTAL_CODE.
# Uses a throwaway IKEA_CLI_HOME so your own settings and session are untouched.
set -u
cd "$(dirname "$0")/.."
market="${1:-ca/en}"
country="${market%%/*}"
language="${market#*/}"
[ "$language" = "$market" ] && language=""
export IKEA_CLI_HOME="$(mktemp -d)"
trap 'rm -rf "$IKEA_CLI_HOME"' EXIT
account_token="${IKEA_ACCESS_TOKEN:-}"
unset IKEA_ACCESS_TOKEN IKEA_COUNTRY IKEA_LANGUAGE IKEA_STORE IKEA_POSTAL_CODE

pass=0
fail=0
run() {
  local expect="$1"; shift
  local out code
  out="$(uv run --quiet ikea --json "$@" 2>/dev/null)"
  code=$?
  if [ "$code" = "$expect" ]; then
    pass=$((pass + 1)); printf 'ok    %-45s exit %s\n' "$*" "$code"
  else
    fail=$((fail + 1)); printf 'FAIL  %-45s exit %s (want %s)\n' "$*" "$code" "$expect"
    printf '%s\n' "$out" | head -c 400; echo
  fi
  sleep 0.5
}
# Like run 0, but a feature IKEA doesn't offer in this market (error.code not_available) counts as "n/a".
na=0
run_or_na() {
  local out code
  out="$(uv run --quiet ikea --json "$@" 2>/dev/null)"
  code=$?
  if [ "$code" = 0 ]; then
    pass=$((pass + 1)); printf 'ok    %-45s exit 0\n' "$*"
  elif printf '%s' "$out" | grep -q '"not_available"'; then
    na=$((na + 1)); printf 'n/a   %-45s not offered in this market\n' "$*"
  else
    fail=$((fail + 1)); printf 'FAIL  %-45s exit %s (want 0)\n' "$*" "$code"
    printf '%s\n' "$out" | head -c 400; echo
  fi
  sleep 0.5
}
q() { uv run --quiet ikea --json "$@" 2>/dev/null; }
pick() { python3 -c "import json,sys; d=json.load(sys.stdin).get('data'); print($1)" 2>/dev/null; }

echo "market: $market"
run 7 search LACK                          # no market chosen yet: told to pick one
run 0 markets
run 0 config set country "$country"
[ -n "$language" ] && run 0 config set language "$language"
run 0 config show
run 0 config path

# Sample data from IKEA itself.
items="$(q search LACK -n 20 | pick "' '.join(i['item_no'] for i in d['items'] if i['item_type']=='ART' and i['price'] and i['online_sellable'] is not False)")"
[ -z "$items" ] && items="$(q search KALLAX -n 20 | pick "' '.join(i['item_no'] for i in d['items'] if i['item_type']=='ART' and i['price'] and i['online_sellable'] is not False)")"
A="$(echo "$items" | cut -d' ' -f1)"
B="$(echo "$items" | cut -d' ' -f2)"
[ -z "$B" ] && B="$A"
url="$(q search LACK -n 1 | pick "d['items'][0]['url']")"
store="${IKEA_SMOKE_STORE:-$(q stores | pick "next((s['id'] for s in d if 'STORE' in str(s.get('kind') or '').upper() and s.get('postal_code')), d[0]['id'])")}"
postal="${IKEA_SMOKE_POSTAL_CODE:-$(q stores | pick "next((s.get('postal_code') or '' for s in d if s['id']=='$store'), '')")}"
category="$(q categories | pick "d[0]['id']")"
echo "items: $A $B  store: $store  postal code: ${postal:-(none)}  category: $category"

run 0 stores
run 0 store set "$store"
run 0 store show
[ -n "$postal" ] && run 0 config set postal_code "$postal"
run 0 search LACK -n 3 --sort price-asc
run 0 search LACK --filters
run 0 search --category "$category" -n 3
run 0 search LACK --sort MOST_POPULAR -n 3
run 0 suggest kal
run 0 categories
run 0 product "$A"
[ -n "$url" ] && run 0 product "$url" --quick
run 4 product 00000001
run 0 stock "$A" "$B"
run 0 stock "$A" --all
if [ -n "$postal" ]; then
  run_or_na delivery "$A:2" "$B"
else
  run 2 delivery "$A:2"                    # explains that a postal code is needed
fi
run 0 auth status
run 0 cart show
run 0 cart add "$A:2" "$B"
run 0 cart set "$A:3"
if [ "$A" != "$B" ]; then
  run 0 cart remove "$B"
  run 4 cart remove "$B"
fi
run 5 cart coupon add NOT-A-REAL-CODE
[ -n "$postal" ] && run_or_na delivery
run 0 cart clear --yes
run 0 lists
run 0 lists create "smoke test"
run 0 lists add "$A:2" --list "smoke test"
run 0 lists set "$A:1" --list "smoke test"
run 0 lists show "smoke test"
run 0 lists rename "smoke test" "smoke test 2"
run 0 lists select "smoke test 2"
run 0 lists remove "$A" --list "smoke test 2"
run 0 lists delete "smoke test 2" --yes
run 3 orders
if [ "$(uv run --quiet python -c "from ikea_cli import markets; print(bool(markets.get('$country').auth_domain))")" = "True" ]; then
  run 0 auth login --start
else
  run 7 auth login --start                 # login not offered for this market
fi
run 0 auth logout --local-only
run 0 cart show

if [ -n "$account_token" ]; then
  export IKEA_ACCESS_TOKEN="$account_token"
  run 0 auth status
  run 0 cart show
  run 0 lists
  run 0 orders -n 5
  run 0 loyalty
  run 0 rewards
  run 0 profile
  first="$(q orders -n 1 | pick "d['purchases'][0]['id'] if d['purchases'] else ''")"
  if [ -n "$first" ]; then
    run 0 orders show "$first"
    run 0 orders status "$first"
  fi
fi

echo "$market passed: $pass  failed: $fail  not offered: $na"
[ "$fail" = 0 ]
