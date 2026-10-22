# AGENTS.md

This repository builds `ikea-cli`, an unofficial command-line client and Python library for the IKEA website (Canada tested). It covers search, products, stores, stock, delivery options, the shopping cart, favourites lists, purchases and IKEA Family rewards. It never places orders or takes payment.

## Layout

- `src/ikea_cli/config.py`: settings, market constants, public client ids, file locations, atomic `0600` writes.
- `src/ikea_cli/transport.py`: the only place that sends HTTP requests; headers, retries for idempotent calls, and turning failures into `IkeaError`.
- `src/ikea_cli/auth.py`: Auth0 PKCE login (copy-paste flow), session storage, refresh with rotation under a file lock, revoke, guest tokens.
- `src/ikea_cli/queries.py`: GraphQL documents for purchases and favourites.
- `src/ikea_cli/normalize.py`: converts raw IKEA responses into the stable dictionaries returned by the library and printed by `--json`.
- `src/ikea_cli/client.py`: `IkeaClient`, one method per user action.
- `src/ikea_cli/cli.py` and `output.py`: argument parsing, human-readable tables, JSON envelope, exit codes.
- `skills/ikea/SKILL.md`: agent-facing usage guide, shipped inside the wheel.

## Rules

- Run `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` before committing. Keep code typed.
- Tests are offline: use `httpx.MockTransport` (see `tests/conftest.py`) and small hand-made fixtures in `tests/fixtures`. Never record real responses into fixtures, and never read real tokens in tests. Live checks live in `tests/test_live.py`, are marked `live` and run only with `IKEA_LIVE=1`.
- New output fields are additive. Don't rename or remove keys in `normalize.py` results; scripts depend on them.
- Errors raise an `IkeaError` subclass with a clear message and, when useful, a `hint` telling the user what to run next.
- Security: never print, log or put tokens, cookies or refresh tokens in error messages or JSON output. Accept secrets from stdin or files, never from command-line arguments. Session files stay `0600`. Refresh tokens rotate, so refreshes must hold the session lock and save atomically.
- Do not add checkout, payment, order placement, order cancellation or point redemption.
- Prefer the standard library. Current runtime dependencies are `httpx` and `platformdirs`; add one only when clearly necessary.
- Repository text is English. Commit messages use Conventional Commits. License: AGPL-3.0-only.
