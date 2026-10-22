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
- Supporting a new market? Run the live checks with `--country/--language` settings for it and report what works. Only add it to `VERIFIED_MARKETS` after the live checks pass.
- Out of scope: checkout, payments, placing or cancelling orders, spending reward points.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
