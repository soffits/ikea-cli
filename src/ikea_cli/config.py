"""Settings, market constants and on-disk locations."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs

from .errors import ConfigError

APP_NAME = "ikea-cli"

# Public identifiers the IKEA website itself sends from the browser. They are not secrets.
GUEST_CLIENT_ID = "b477b9e4-b836-40fb-842f-43db89dd3c52"
STOCK_CLIENT_ID = "ef382663-a2a5-40d4-8afe-f0634821c0ed"
CART_BFF_CLIENT_ID = "c3077c7d-d57c-4206-9a77-a55e01211b1b"
LOCATION_CLIENT_ID = "ffe52bc4-447d-4e2b-bda2-9b51fc63873c"
PURCHASES_CLIENT_ID = "93c183aa-8e15-4e80-8cbe-f812254c7933"
FAVOURITES_CLIENT_ID = "fe88ae57-2c34-4b14-8979-b915872886f7"


@dataclass(frozen=True)
class AuthMarket:
    """Auth0 settings for one market's IKEA account login."""

    domain: str
    client_id: str
    audience: str = "https://retail.api.ikea.com"
    scope: str = "openid profile email offline_access"


# Markets whose login has been checked. Other markets can be configured with
# `auth_domain` and `auth_client_id` in the config file.
KNOWN_AUTH: dict[str, AuthMarket] = {
    "ca": AuthMarket(domain="ca.accounts.ikea.com", client_id="jUflsyuJkwbC0RQg1i8bgo1dyld6NM5d"),
}

VERIFIED_MARKETS = {("ca", "en"), ("ca", "fr")}

_CODE_RE = re.compile(r"^[a-z]{2}$")
_STORE_RE = re.compile(r"^[0-9]{3,5}$")


@dataclass
class Settings:
    country: str = "ca"
    language: str = "en"
    store: str | None = None
    postal_code: str | None = None
    auth_domain: str | None = None
    auth_client_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.country = self.country.lower()
        self.language = self.language.lower()
        if not _CODE_RE.match(self.country):
            raise ConfigError(f"Invalid country code {self.country!r}; use a two-letter code such as 'ca'.")
        if not _CODE_RE.match(self.language):
            raise ConfigError(f"Invalid language code {self.language!r}; use a two-letter code such as 'en'.")
        if self.store is not None:
            self.store = str(self.store)
            if not _STORE_RE.match(self.store):
                raise ConfigError(f"Invalid store id {self.store!r}; store ids are numbers such as '149'.")
        if self.postal_code:
            self.postal_code = normalize_postal_code(self.postal_code)

    @property
    def locale(self) -> str:
        return f"{self.language}-{self.country.upper()}"

    @property
    def verified(self) -> bool:
        return (self.country, self.language) in VERIFIED_MARKETS

    def auth_market(self) -> AuthMarket:
        known = KNOWN_AUTH.get(self.country)
        domain = self.auth_domain or (known.domain if known else None)
        client_id = self.auth_client_id or (known.client_id if known else None)
        if not domain or not client_id:
            raise ConfigError(
                f"Login is not configured for market {self.country!r}.",
                hint="Set auth_domain and auth_client_id with `ikea config set` (see the README).",
            )
        return AuthMarket(domain=domain, client_id=client_id)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra")
        return data


SETTABLE_KEYS = ("country", "language", "store", "postal_code", "auth_domain", "auth_client_id")


def normalize_postal_code(value: str) -> str:
    return " ".join(value.upper().split())


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def guest_file(self) -> Path:
        return self.state_dir / "guest.json"

    @classmethod
    def default(cls) -> Paths:
        base = os.environ.get("IKEA_CLI_HOME")
        if base:
            root = Path(base).expanduser()
            return cls(root / "config", root / "state", root / "cache")
        return cls(
            Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)),
            Path(platformdirs.user_state_dir(APP_NAME, appauthor=False)),
            Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False)),
        )


def load_settings(paths: Paths, overrides: dict[str, Any] | None = None) -> Settings:
    """Read the config file, then apply IKEA_* environment variables, then explicit overrides."""

    data: dict[str, Any] = {}
    if paths.config_file.exists():
        try:
            data = json.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read config file {paths.config_file}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Config file {paths.config_file} must contain a JSON object.")
    env_map = {
        "IKEA_COUNTRY": "country",
        "IKEA_LANGUAGE": "language",
        "IKEA_STORE": "store",
        "IKEA_POSTAL_CODE": "postal_code",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            data[key] = os.environ[env]
    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value
    known = {f.name for f in fields(Settings)} - {"extra"}
    kwargs = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return Settings(**kwargs, extra=extra)


def save_config(paths: Paths, values: dict[str, Any]) -> None:
    write_private_json(paths.config_file, values)


def read_config_file(paths: Paths) -> dict[str, Any]:
    if not paths.config_file.exists():
        return {}
    try:
        data = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config file {paths.config_file}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_private_json(path: Path, data: Any) -> None:
    """Write JSON atomically with 0600 permissions (directory 0700)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with contextlib.suppress(OSError, AttributeError):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_private_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
