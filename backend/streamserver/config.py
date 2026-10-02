"""Configuration loading for the Stream server.

Configuration comes from three layers, later layers winning:

1. ``/etc/streamserver/server.toml`` (or the path in ``STREAMSERVER_CONFIG``)
2. the directory named by ``STREAMSERVER_DATA_DIR``
3. explicit ``STREAMSERVER_*`` environment variables
4. command line flags

Every path the server writes to lives under the data directory, and every path
it reads media from must be declared as a library root -- there is no way to
reach the filesystem by passing a path to the API.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

DEFAULT_DATA_DIR = Path(
    os.environ.get("STREAMSERVER_DATA_DIR", Path.home() / ".local" / "share" / "streamserver")
)
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("STREAMSERVER_CONFIG", "/etc/streamserver/server.toml")
)


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


@dataclass(slots=True)
class ServerConfig:
    """Network-facing settings."""

    host: str = "127.0.0.1"
    port: int = 8484
    #: Origin header accepted for browser clients.  ``None`` disables CORS
    #: entirely, which is the default: the native client does not need it and
    #: every allowed origin is one more way for a hostile page to ride along on
    #: an authenticated session.
    cors_origins: list[str] = field(default_factory=list)
    #: Trusted reverse proxies, one per line in TOML.  Only used to honour
    #: X-Forwarded-* when the immediate peer is listed here.
    trusted_proxies: list[str] = field(default_factory=list)
    #: Hostnames this server answers to.  Empty means "derive from the bind
    #: address and this machine's name"; a server bound to a routable address
    #: falls back to accepting any Host header, which is why listing the names
    #: a LAN or Tailscale deployment is reached by is worth doing.
    allowed_hosts: list[str] = field(default_factory=list)
    #: Serve over plain HTTP.  Only ever true for a LAN/Tailscale deployment
    #: that already sits behind a tunnel doing TLS termination.
    allow_insecure_transport: bool = False
    request_timeout_seconds: int = 60


@dataclass(slots=True)
class AuthConfig:
    """Token lifetimes and password policy."""

    access_token_ttl_seconds: int = 900          # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    #: A remembered login survives this long without a refresh.
    refresh_token_ttl_long_seconds: int = 60 * 60 * 24 * 365
    max_failed_logins: int = 8
    lockout_seconds: int = 300
    #: Force a password change for accounts still on the shipped default.
    require_password_change: bool = True
    password_min_length: int = 10
    #: Argon2id parameters would be preferable, but hashlib is in the standard
    #: library on every interpreter we target while argon2/bcrypt are optional
    #: native dependencies.  scrypt with these parameters costs ~64 MiB and
    #: ~100 ms on the target hardware, which is the right order of magnitude.
    scrypt_n: int = 2 ** 15
    scrypt_r: int = 8
    scrypt_p: int = 1
    scrypt_maxmem: int = 128 * 1024 * 1024


@dataclass(slots=True)
class MediaConfig:
    """Media roots and playback behaviour."""

    #: Directories that may contain media.  Nothing outside these is reachable.
    library_roots: list[str] = field(default_factory=list)
    #: Bytes served per chunk by the Range proxy.  8 MiB keeps the 4 GB-RAM box
    #: from buffering a whole film while staying large enough that the client
    #: is not issuing a request every 200 ms.
    stream_chunk_size: int = 8 * 1024 * 1024
    max_range_span: int = 512 * 1024 * 1024
    #: Below this many seconds remaining, "finished" is reported instead.
    completion_threshold: float = 0.97
    #: Position within the first N seconds counts as "not started".
    start_threshold_seconds: float = 5.0
    # ffprobe integration
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: int = 30


@dataclass(slots=True)
class RateLimitConfig:
    """Token-bucket ceilings, expressed per minute unless stated."""

    login_per_minute: int = 10
    login_lockout_per_minute: int = 30
    api_per_minute: int = 600
    stream_per_minute: int = 120
    search_per_minute: int = 120
    #: Bulk library work: import/rescan, deep verify, reindex, backup.  Each of
    #: these walks the whole tree or rewrites the whole index, so the generic
    #: 600/min api ceiling is not a meaningful bound on them.
    import_per_minute: int = 30
    maintenance_per_minute: int = 12
    #: PIN guesses per profile.  A 4-digit PIN is 10^4, so this is the only
    #: thing standing between a stolen session and a kids profile.
    pin_attempts_per_minute: int = 6


@dataclass(slots=True)
class MetadataConfig:
    """Where descriptions, posters and ratings come from."""

    #: ``local`` reads .nfo/.json sidecars and nothing else.  ``tmdb`` and
    #: ``imdb`` require an API key that is supplied at runtime, never baked in.
    default_provider: str = "local"
    cache_ttl_days: int = 30
    request_timeout_seconds: int = 15
    tmdb_api_key: str = ""
    imdb_api_key: str = ""
    #: Hard ceiling on poster/backdrop downloads so a hostile provider cannot
    #: fill the 10 GB SSD.
    max_image_bytes: int = 12 * 1024 * 1024


@dataclass(slots=True)
class StorageConfig:
    """Where the database, images and logs live.

    On the target box the database sits on the 10 GB SSD and only the media
    files live on the slow 1 TB HDD, so ``data_dir`` should be on the SSD.
    """

    data_dir: Path = DEFAULT_DATA_DIR
    database_path: Path | None = None      # defaults to data_dir/streamserver.db
    image_dir: Path | None = None           # defaults to data_dir/images
    log_dir: Path | None = None            # defaults to data_dir/logs
    backup_dir: Path | None = None         # defaults to data_dir/backups
    log_level: str = "INFO"
    log_to_file: bool = True
    #: Audit log retention.  Security-relevant, never auto-pruned below this.
    audit_retention_days: int = 180


@dataclass(slots=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    source_path: Path | None = None

    # -- derived paths ----------------------------------------------------
    # Each optional override *defaults to None*, meaning "derive it from
    # data_dir".  The ``or`` matters: a relative value in an override would
    # otherwise be returned verbatim, and validation has just rejected those.
    @property
    def database_file(self) -> Path:
        return self.storage.database_path or (self.storage.data_dir / "streamserver.db")

    @property
    def image_path(self) -> Path:
        return self.storage.image_dir or (self.storage.data_dir / "images")

    @property
    def log_path(self) -> Path:
        return self.storage.log_dir or (self.storage.data_dir / "logs")

    @property
    def backup_path(self) -> Path:
        return self.storage.backup_dir or (self.storage.data_dir / "backups")

    @property
    def data_root(self) -> Path:
        """The data directory itself, validated."""
        return Path(self.storage.data_dir)

    def ensure_directories(self) -> None:
        for path in (
            self.storage.data_dir,
            self.database_file.parent,
            self.image_path,
            self.log_path,
            self.backup_path,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def library_root_paths(self) -> list[Path]:
        roots: list[Path] = []
        for entry in self.media.library_roots:
            path = Path(entry).expanduser()
            if not path.is_absolute():
                raise ConfigError(f"media.library_roots entry must be absolute: {entry!r}")
            roots.append(path.resolve(strict=False))
        return roots


_ENV_PREFIX = "STREAMSERVER_"
_SECTIONS = {
    "server": ServerConfig,
    "auth": AuthConfig,
    "media": MediaConfig,
    "rate_limit": RateLimitConfig,
    "metadata": MetadataConfig,
    "storage": StorageConfig,
}


def _coerce(value: Any, target_type: Any, dotted: str) -> Any:
    origin = getattr(target_type, "__origin__", None)
    # ``list[str]`` arrives as the bare ``list`` class once ``typing.List[str]``
    # has been resolved, so membership is tested against both forms.
    if origin is list or target_type is list:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            raise ConfigError(f"{dotted}: expected a list, got {type(value).__name__}")
        return [str(v) for v in value]
    if target_type is bool or origin is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        if isinstance(value, int):
            return bool(value)
        raise ConfigError(f"{dotted}: expected a boolean, got {value!r}")
    if target_type is int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{dotted}: expected an integer, got {value!r}") from exc
    if target_type is float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{dotted}: expected a number, got {value!r}") from exc
    if target_type is Path:
        return Path(str(value)).expanduser()
    if target_type is str:
        return str(value)
    raise ConfigError(f"{dotted}: unsupported configuration type {target_type!r}")


def _apply(section: object, raw: dict[str, Any], dotted_prefix: str) -> None:
    valid = {f.name: f.type for f in fields(section)} if is_dataclass(section) else {}
    for key, value in raw.items():
        if key not in valid:
            raise ConfigError(f"{dotted_prefix}{key}: unknown configuration key")
        annotated = section.__class__.__annotations__.get(key, valid[key])
        # ``from __future__ import annotations`` turns these into strings.
        resolved = {
            "str": str, "int": int, "float": float, "bool": bool,
            "list[str]": list, "Path": Path, "Path | None": Path, "None": str,
        }.get(annotated if isinstance(annotated, str) else "", None)
        if resolved is None:
            resolved = valid[key]
        setattr(section, key, _coerce(value, resolved, f"{dotted_prefix}{key}"))


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise ConfigError(
            "No TOML parser available. Install tomli on Python < 3.11, or use "
            "Python 3.11+."
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced with the path
        raise ConfigError(f"{path}: invalid TOML ({exc})") from exc


def _load_env(config: Config, environ: dict[str, str]) -> None:
    """Map ``STREAMSERVER_SECTION_KEY`` onto the matching dataclass field."""
    for section_name, section_cls in _SECTIONS.items():
        section = getattr(config, section_name)
        type_hints = {
            f.name: f.type for f in fields(section)
        }
        for field_name, field_type in type_hints.items():
            env_key = f"{_ENV_PREFIX}{section_name.upper()}_{field_name.upper()}"
            if env_key not in environ:
                continue
            hint = section_cls.__annotations__.get(field_name, field_type)
            resolved = {
                "str": str, "int": int, "float": float, "bool": bool,
                "list[str]": list, "Path": Path, "Path | None": Path,
            }.get(hint if isinstance(hint, str) else "", field_type)
            setattr(section, field_name, _coerce(environ[env_key], resolved, env_key))


def _is_valid_host(value: str) -> bool:
    """Accept an IPv4/IPv6 literal or a DNS name.  No scheme, path or port."""
    import ipaddress
    import re

    if any(ch in value for ch in " /\t\r\n?#@"):
        return False
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        pass
    return bool(re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                             r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", value))


def _validate(config: Config) -> None:
    if not (1 <= config.server.port <= 65535):
        raise ConfigError(f"server.port out of range: {config.server.port}")
    if config.auth.access_token_ttl_seconds >= config.auth.refresh_token_ttl_seconds:
        raise ConfigError("auth.access_token_ttl_seconds must be shorter than the refresh TTL")
    if config.auth.password_min_length < 8:
        raise ConfigError("auth.password_min_length must be at least 8")
    if config.media.stream_chunk_size < 64 * 1024:
        raise ConfigError("media.stream_chunk_size must be at least 64 KiB")
    for path in config.storage.data_dir, config.database_file, config.image_path:
        # database_path and image_dir default to None, which means "derive
        # from data_dir"; an explicit relative value is still a mistake.
        if path is not None and not path.is_absolute():
            raise ConfigError(f"storage paths must be absolute: {path}")
    if config.server.host and not _is_valid_host(config.server.host):
        raise ConfigError(f"server.host is not a valid address or hostname: {config.server.host!r}")
    if config.metadata.default_provider not in {"local", "tmdb", "imdb", "none"}:
        raise ConfigError(f"unknown metadata provider: {config.metadata.default_provider!r}")


def load_config(
    path: Path | str | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    required: bool = False,
) -> Config:
    """Build a :class:`Config` from file + environment + explicit overrides."""
    environ = dict(os.environ if environ is None else environ)
    config = Config()

    candidate = Path(path) if path else DEFAULT_CONFIG_PATH
    if candidate.exists():
        raw = _load_toml(candidate)
        config.source_path = candidate
        for section_name, raw_section in raw.items():
            if section_name not in _SECTIONS:
                raise ConfigError(f"{candidate}: unknown section [{section_name}]")
            if not isinstance(raw_section, dict):
                raise ConfigError(f"{candidate}: [{section_name}] must be a table")
            _apply(getattr(config, section_name), raw_section, f"{section_name}.")
    elif required:
        raise ConfigError(f"configuration file not found: {candidate}")

    env_data_dir = environ.get(f"{_ENV_PREFIX}DATA_DIR")
    if env_data_dir:
        config.storage.data_dir = Path(env_data_dir).expanduser()

    _load_env(config, environ)

    if overrides:
        for dotted, value in overrides.items():
            section_name, _, key = dotted.partition(".")
            if section_name not in _SECTIONS:
                raise ConfigError(f"unknown override section: {section_name!r}")
            if not key:
                raise ConfigError(f"override must be SECTION.KEY, got {dotted!r}")
            _apply(getattr(config, section_name), {key: value}, f"{section_name}.")

    _validate(config)
    return config


def dump_config(config: Config) -> str:
    """Render a config back to TOML -- used by ``streamserver init-config``.

    Optional paths that were never set are omitted rather than written as
    ``""``: a reader must be able to tell "not configured" from "configured as
    an empty path", and the derived-path properties rely on that distinction.
    Every scalar is quoted, which is valid TOML for integers, floats and
    booleans too and keeps a hand-edited file uniform.
    """
    lines: list[str] = []
    for section_name in _SECTIONS:
        section = getattr(config, section_name)
        lines.append(f"[{section_name}]")
        for f in fields(section):
            value = getattr(section, f.name)
            if isinstance(value, Path):
                if value is None:
                    continue
                rendered = f'"{value}"'
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, list):
                rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
            elif value is None:
                continue
            else:
                rendered = f'"{value}"'
            lines.append(f"{f.name} = {rendered}")
        lines.append("")
    return "\n".join(lines)