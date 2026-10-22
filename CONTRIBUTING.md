# Contributing

Thanks for helping improve ikea-cli.

## Setup

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

## Guidelines

- Keep changes small and covered by tests. Tests must run offline: mock HTTP with `httpx.MockTransport` and write small, made-up fixtures. Don't paste real API responses, tokens, order numbers, names, addresses or postal codes into the repository, issues or pull requests.
- If IKEA changed something, describe the request and the new response shape (with personal data removed) in the pull request, and update `normalize.py` so the JSON output keeps its existing keys.
- `tests/test_live.py` holds optional checks against the real website (`IKEA_LIVE=1 uv run pytest -m live`). They use guest sessions only. Keep them polite: a handful of requests, no loops.
- Markets: `src/ikea_cli/markets.json` is generated; don't edit it by hand. Run `uv run --with pycountry scripts/update_markets.py` (or `--only us,de`) to refresh it from IKEA's pages. To check a market live, run `scripts/live-smoke.sh CC/LANG` and `IKEA_LIVE=1 IKEA_LIVE_MARKETS=CC/LANG uv run pytest -m live`, then record the date in `markets.TESTED`.
- Out of scope: checkout, payments, placing or cancelling orders, spending reward points.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
