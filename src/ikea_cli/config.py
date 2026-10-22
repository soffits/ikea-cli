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

from . import markets
from .errors import ConfigError, MarketNotSet

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


_CODE_RE = re.compile(r"^[a-z]{2}$")
_STORE_RE = re.compile(r"^[0-9]{3,5}$")


@dataclass
class Settings:
    """Where and how to talk to IKEA.

    ``country`` has no default: IKEA runs a separate shop per country, so it must be chosen
    (see ``ikea markets``). ``language`` defaults to the market's main language.
    """

    country: str | None = None
    language: str | None = None
    store: str | None = None
    postal_code: str | None = None
    auth_domain: str | None = None
    auth_client_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
    # True when no country was configured and the 0.1 default (Canada) was applied for an existing setup.
    implicit_country: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.country is not None:
            self.country = str(self.country).strip().lower()
            if not _CODE_RE.match(self.country):
                raise ConfigError(f"Invalid country code {self.country!r}; use a two-letter code such as 'us' or 'de'.")
        if self.language is not None:
            self.language = str(self.language).strip().lower()
            if not _CODE_RE.match(self.language):
                raise ConfigError(f"Invalid language code {self.language!r}; use a two-letter code such as 'en'.")
        elif self.country:
            market = markets.get(self.country)
            if market is not None:
                self.language = market.default_language
        if self.store is not None:
            self.store = str(self.store)
            if not _STORE_RE.match(self.store):
                raise ConfigError(f"Invalid store id {self.store!r}; store ids are numbers (see `ikea stores`).")
        if self.postal_code:
            self.postal_code = normalize_postal_code(self.postal_code)

    def market(self) -> markets.Market:
        """The configured market. Raises if none is set, or if ikea-cli cannot serve it."""

        if not self.country:
            raise MarketNotSet()
        market, language = markets.resolve(self.country, self.language)
        self.language = language
        return market

    @property
    def locale(self) -> str:
        return f"{self.language or 'en'}-{(self.country or '').upper()}"

    @property
    def verified(self) -> bool:
        market = markets.get(self.country)
        return bool(market and market.tested and self.language in market.languages)

    def auth_market(self) -> AuthMarket:
        market = self.market()
        domain = self.auth_domain or market.auth_domain
        client_id = self.auth_client_id or market.auth_client_id
        if not domain or not client_id:
            raise ConfigError(
                f"Logging in from ikea-cli is not available for IKEA {market.name}: its website signs in a different way.",
                hint="Search, stock, delivery and the guest cart and lists still work. If you know the market's Auth0 "
                "settings, set auth_domain and auth_client_id with `ikea config set`.",
            )
        return AuthMarket(domain=domain, client_id=client_id)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra")
        data.pop("implicit_country")
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
    implicit = False
    if not data.get("country") and _used_before_markets(paths):
        # ikea-cli 0.1 only served Canada and did not store the country; keep existing setups working.
        data["country"] = LEGACY_COUNTRY
        implicit = True
    known = {f.name for f in fields(Settings)} - {"extra", "implicit_country"}
    kwargs = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return Settings(**kwargs, extra=extra, implicit_country=implicit)


LEGACY_COUNTRY = "ca"


def _used_before_markets(paths: Paths) -> bool:
    """True if this computer has settings or sessions from a version that always used Canada."""

    if paths.config_file.exists():
        return True
    if (paths.state_dir / f"session-{LEGACY_COUNTRY}.json").exists():
        return True
    guest = read_private_json(paths.guest_file)
    return isinstance(guest, dict) and guest.get("country") == LEGACY_COUNTRY


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
