# Security policy

## Reporting

Please report vulnerabilities privately through GitHub's security advisory form for this repository. If that is not possible, open a minimal issue asking for a contact, without exploit details.

Never include access tokens, refresh tokens, `auth0` cookies, order numbers, names, email addresses, postal codes or other personal data in reports.

## How ikea-cli handles your account

- ikea-cli never sees your IKEA password. You sign in on ikea.com in your browser; the CLI receives a one-time authorization code bound to its own PKCE secret and exchanges it for tokens.
- Tokens are stored per market in the platform state directory (or `IKEA_CLI_HOME`), in files with mode `0600` inside a `0700` directory, written atomically. Refresh tokens rotate on every use; refreshes, logins and logouts are serialized with a file lock (`flock` on Linux and macOS, `msvcrt.locking` on Windows, which is not yet covered by CI) so parallel commands don't invalidate each other. The new refresh token is written to disk before anything else is processed, and Ctrl-C is held back for that short step.
- Tokens and cookies are never printed, logged or included in error messages or `--json` output. Secrets are read from stdin or files, never from command-line arguments, so they don't end up in shell history or process lists.
- An `auth0` cookie given to `ikea auth import --cookie` is used for one request and not stored.
- `ikea auth logout` revokes the refresh token at IKEA and deletes the local session and guest files.
- The client ids in the source are public identifiers that IKEA's website sends from every browser; they are not secrets.

## Supported versions

Security fixes go into the latest release.
