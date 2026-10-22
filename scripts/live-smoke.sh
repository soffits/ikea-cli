#!/usr/bin/env bash
# Run every ikea command once against the real IKEA website (Canada), as a guest.
# Account commands run read-only only if IKEA_ACCESS_TOKEN is set; nothing in the account is changed.
#
#   scripts/live-smoke.sh            # guest only
#   IKEA_ACCESS_TOKEN=... scripts/live-smoke.sh
#
# Uses a throwaway IKEA_CLI_HOME so your own settings and session are untouched.
set -u
cd "$(dirname "$0")/.."
export IKEA_CLI_HOME="$(mktemp -d)"
trap 'rm -rf "$IKEA_CLI_HOME"' EXIT
account_token="${IKEA_ACCESS_TOKEN:-}"
unset IKEA_ACCESS_TOKEN

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

LACK=30449908
RUG=10623220

run 0 config show
run 0 config path
run 0 config set postal_code "M5V 3L9"
run 0 stores
run 0 store set 149
run 0 store show
run 0 search desk -n 3 --sort price-asc
run 0 search desk -n 3 -f colors=10139 -f new-product
run 0 search desk --filters
run 0 search --category 10653 -n 3
run 0 search white --category 10653 -n 3
run 0 search desk --sort MOST_POPULAR -n 3
run 0 suggest kal
run 0 categories rug
run 0 product "$LACK"
run 0 product https://www.ikea.com/ca/en/p/lagkapten-spaend-desk-white-s09563608/ --quick
run 4 product 00000001
run 0 stock "$LACK" "$RUG"
run 0 stock "$LACK" --all
run 0 delivery "$LACK:2" "$RUG"
run 0 auth status
run 0 cart show
run 0 cart add "$LACK:2" "$RUG"
run 0 cart set "$LACK:3"
run 0 cart remove "$RUG"
run 4 cart remove "$RUG"
run 5 cart coupon add NOT-A-REAL-CODE
run 0 delivery
run 0 cart clear --yes
run 0 lists
run 0 lists create "smoke test"
run 0 lists add "$LACK:2" --list "smoke test"
run 0 lists set "$LACK:1" --list "smoke test"
run 0 lists show "smoke test"
run 0 lists rename "smoke test" "smoke test 2"
run 0 lists select "smoke test 2"
run 0 lists remove "$LACK" --list "smoke test 2"
run 0 lists delete "smoke test 2" --yes
run 3 orders
run 0 auth login --start
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
  first="$(uv run --quiet ikea --json orders -n 1 | python3 -c 'import json,sys; p=json.load(sys.stdin)["data"]["purchases"]; print(p[0]["id"] if p else "")')"
  if [ -n "$first" ]; then
    run 0 orders show "$first"
    run 0 orders status "$first"
  fi
fi

echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
