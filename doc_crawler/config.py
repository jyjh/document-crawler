from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


ALLOWED_PLACEHOLDERS = {
    "base_url",
    "hash",
    "filename",
    "filepath",
    "filesize",
    "mtime_iso",
}
SUPPORTED_HASHES = {"sha256", "sha1", "md5"}
CHECK_DETECT_MODES = {"status_map", "json_path", "always_exists"}
UPLOAD_DETECT_MODES = {"status_in", "json_path"}
UPLOAD_FORMATS = {"multipart", "raw", "json"}
ON_UNKNOWN = {"error", "treat_missing", "skip"}
SERVER_UNREACHABLE = {"fail", "skip"}
RETRY_KINDS = {"5xx", "timeout", "connection_error"}

_ENV_RE = re.compile(r"\$\{([^}]+)\}")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_FILE_RE = re.compile(r"file:([^\s]+)")


class ConfigError(ValueError):
    """Raised when the YAML config is invalid."""


@dataclass(frozen=True)
class CrawlConfig:
    directories: tuple[str, ...]
    extensions: tuple[str, ...]
    recursive: bool = True
    exclude_dirs: tuple[str, ...] = ()
    max_file_size_mb: int | None = None

    @property
    def max_file_size_bytes(self) -> int | None:
        if self.max_file_size_mb is None:
            return None
        return int(self.max_file_size_mb * 1024 * 1024)


@dataclass(frozen=True)
class HashConfig:
    algorithm: str = "sha256"


@dataclass(frozen=True)
class ServerConfig:
    base_url: str
    timeout_seconds: float = 30
    verify_tls: bool = True
    ca_bundle: str | None = None
    headers: Mapping[str, str] | None = None

    @property
    def verify(self) -> bool | str:
        return self.ca_bundle if self.ca_bundle else self.verify_tls


@dataclass(frozen=True)
class DetectConfig:
    mode: str
    status_map: Mapping[str, tuple[int, ...]] | None = None
    status_in: tuple[int, ...] | None = None
    json_path: str | None = None
    json_path_truthy: bool = True
    json_path_eq: Any = None


@dataclass(frozen=True)
class CheckConfig:
    method: str
    url: str
    query: Mapping[str, Any] | None
    json_body: Mapping[str, Any] | None
    detect: DetectConfig
    on_unknown: str = "error"
    retry_on: tuple[str, ...] = ("5xx", "timeout", "connection_error")


@dataclass(frozen=True)
class UploadConfig:
    method: str
    url: str
    format: str
    file_field: str
    filename_template: str
    extra_fields: Mapping[str, Any]
    detect: DetectConfig
    idempotent: bool = False
    retry_on: tuple[str, ...] = ("5xx", "timeout", "connection_error")


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 3
    backoff_seconds: float = 2


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    path: str = ".doc_crawler_cache.sqlite"
    trust_size_mtime: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "logs/crawler.log"
    console: bool = True


@dataclass(frozen=True)
class AppConfig:
    config_path: str
    config_dir: str
    crawl: CrawlConfig
    hash: HashConfig
    server: ServerConfig
    check: CheckConfig
    upload: UploadConfig
    retry: RetryConfig
    server_unreachable: str
    cache: CacheConfig
    logging: LoggingConfig


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    config_dir = config_path.parent
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError(f"Unable to read config: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    expanded = _expand_load_time(raw, config_dir)
    _validate_placeholders(expanded)

    crawl = _load_crawl(_section(expanded, "crawl"))
    hash_cfg = _load_hash(_section(expanded, "hash", required=False))
    server = _load_server(_section(expanded, "server"), config_dir)
    check = _load_check(_section(expanded, "check"))
    upload = _load_upload(_section(expanded, "upload"))
    retry = _load_retry(_section(expanded, "retry", required=False))
    cache = _load_cache(_section(expanded, "cache", required=False), config_dir)
    logging_cfg = _load_logging(_section(expanded, "logging", required=False), config_dir)

    policy = str(expanded.get("server_unreachable", "fail")).lower()
    if policy not in SERVER_UNREACHABLE:
        raise ConfigError("server_unreachable must be one of: fail, skip")

    return AppConfig(
        config_path=str(config_path),
        config_dir=str(config_dir),
        crawl=crawl,
        hash=hash_cfg,
        server=server,
        check=check,
        upload=upload,
        retry=retry,
        server_unreachable=policy,
        cache=cache,
        logging=logging_cfg,
    )


def render_template(value: Any, ctx: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(_StrictFormat(ctx))
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    if isinstance(value, tuple):
        return tuple(render_template(v, ctx) for v in value)
    return value


def _section(data: Mapping[str, Any], name: str, *, required: bool = True) -> Mapping[str, Any]:
    value = data.get(name)
    if value is None:
        if required:
            raise ConfigError(f"Missing required section: {name}")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _load_crawl(data: Mapping[str, Any]) -> CrawlConfig:
    directories = _as_str_tuple(data.get("directories"), "crawl.directories")
    if not directories:
        raise ConfigError("crawl.directories must contain at least one directory")

    extensions = _as_str_tuple(data.get("extensions", [".pdf"]), "crawl.extensions")
    normalized_exts = []
    for ext in extensions:
        ext = ext.strip().lower()
        if not ext:
            raise ConfigError("crawl.extensions cannot contain empty values")
        normalized_exts.append(ext if ext.startswith(".") else f".{ext}")

    for directory in directories:
        if not Path(directory).expanduser().exists():
            warnings.warn(f"crawl directory does not exist yet: {directory}", RuntimeWarning)

    exclude_dirs = tuple(_normalize_exclude_dir(value) for value in _as_str_tuple(data.get("exclude_dirs", []), "crawl.exclude_dirs"))
    max_file_size_mb = data.get("max_file_size_mb")
    if max_file_size_mb is not None and int(max_file_size_mb) <= 0:
        raise ConfigError("crawl.max_file_size_mb must be positive")

    return CrawlConfig(
        directories=tuple(str(Path(d).expanduser()) for d in directories),
        extensions=tuple(normalized_exts),
        recursive=bool(data.get("recursive", True)),
        exclude_dirs=exclude_dirs,
        max_file_size_mb=int(max_file_size_mb) if max_file_size_mb is not None else None,
    )


def _load_hash(data: Mapping[str, Any]) -> HashConfig:
    algorithm = str(data.get("algorithm", "sha256")).lower()
    if algorithm not in SUPPORTED_HASHES:
        raise ConfigError(f"hash.algorithm must be one of: {', '.join(sorted(SUPPORTED_HASHES))}")
    return HashConfig(algorithm=algorithm)


def _load_server(data: Mapping[str, Any], config_dir: Path) -> ServerConfig:
    base_url = str(data.get("base_url", "")).rstrip("/")
    if not base_url:
        raise ConfigError("server.base_url is required")

    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        raise ConfigError("server.headers must be a mapping")

    ca_bundle = data.get("ca_bundle")
    if ca_bundle:
        ca_bundle = _resolve_relative_path(str(ca_bundle), config_dir)

    return ServerConfig(
        base_url=base_url,
        timeout_seconds=float(data.get("timeout_seconds", 30)),
        verify_tls=bool(data.get("verify_tls", True)),
        ca_bundle=ca_bundle,
        headers={str(k): str(v) for k, v in headers.items()},
    )


def _load_check(data: Mapping[str, Any]) -> CheckConfig:
    query = data.get("query")
    json_body = data.get("json_body")
    if query is not None and json_body is not None:
        raise ConfigError("check.query and check.json_body are mutually exclusive")
    if query is not None and not isinstance(query, dict):
        raise ConfigError("check.query must be a mapping")
    if json_body is not None and not isinstance(json_body, dict):
        raise ConfigError("check.json_body must be a mapping")

    detect = _load_detect(data.get("detect"), CHECK_DETECT_MODES, "check.detect")
    on_unknown = str(data.get("on_unknown", "error")).lower()
    if on_unknown not in ON_UNKNOWN:
        raise ConfigError("check.on_unknown must be one of: error, treat_missing, skip")

    return CheckConfig(
        method=str(data.get("method", "GET")).upper(),
        url=str(data.get("url", "")),
        query=query,
        json_body=json_body,
        detect=detect,
        on_unknown=on_unknown,
        retry_on=_load_retry_on(data.get("retry_on")),
    )


def _load_upload(data: Mapping[str, Any]) -> UploadConfig:
    fmt = str(data.get("format", "multipart")).lower()
    if fmt not in UPLOAD_FORMATS:
        raise ConfigError("upload.format must be one of: multipart, raw, json")

    detect = _load_detect(data.get("detect"), UPLOAD_DETECT_MODES, "upload.detect")
    extra_fields = data.get("extra_fields") or {}
    if not isinstance(extra_fields, dict):
        raise ConfigError("upload.extra_fields must be a mapping")

    return UploadConfig(
        method=str(data.get("method", "POST")).upper(),
        url=str(data.get("url", "")),
        format=fmt,
        file_field=str(data.get("file_field", "file")),
        filename_template=str(data.get("filename_template", "{filename}")),
        extra_fields=extra_fields,
        detect=detect,
        idempotent=bool(data.get("idempotent", False)),
        retry_on=_load_retry_on(data.get("retry_on")),
    )


def _load_detect(data: Any, allowed_modes: set[str], label: str) -> DetectConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be a mapping")
    mode = str(data.get("mode", "")).lower()
    if mode not in allowed_modes:
        raise ConfigError(f"{label}.mode must be one of: {', '.join(sorted(allowed_modes))}")

    status_map = None
    if mode == "status_map":
        raw_map = data.get("status_map")
        if not isinstance(raw_map, dict) or "exists" not in raw_map or "missing" not in raw_map:
            raise ConfigError(f"{label}.status_map requires exists and missing lists")
        status_map = {
            "exists": tuple(int(v) for v in _as_list(raw_map.get("exists"), f"{label}.status_map.exists")),
            "missing": tuple(int(v) for v in _as_list(raw_map.get("missing"), f"{label}.status_map.missing")),
        }

    status_in = None
    if mode == "status_in":
        status_in = tuple(int(v) for v in _as_list(data.get("status_in"), f"{label}.status_in"))
        if not status_in:
            raise ConfigError(f"{label}.status_in cannot be empty")

    json_path = data.get("json_path")
    if mode == "json_path" and not json_path:
        raise ConfigError(f"{label}.json_path is required for json_path mode")

    return DetectConfig(
        mode=mode,
        status_map=status_map,
        status_in=status_in,
        json_path=str(json_path) if json_path else None,
        json_path_truthy=bool(data.get("json_path_truthy", True)),
        json_path_eq=data.get("json_path_eq"),
    )


def _load_retry(data: Mapping[str, Any]) -> RetryConfig:
    attempts = int(data.get("attempts", 3))
    backoff = float(data.get("backoff_seconds", 2))
    if attempts < 1:
        raise ConfigError("retry.attempts must be at least 1")
    if backoff < 0:
        raise ConfigError("retry.backoff_seconds cannot be negative")
    return RetryConfig(attempts=attempts, backoff_seconds=backoff)


def _load_cache(data: Mapping[str, Any], config_dir: Path) -> CacheConfig:
    return CacheConfig(
        enabled=bool(data.get("enabled", True)),
        path=_resolve_relative_path(str(data.get("path", ".doc_crawler_cache.sqlite")), config_dir),
        trust_size_mtime=bool(data.get("trust_size_mtime", True)),
    )


def _load_logging(data: Mapping[str, Any], config_dir: Path) -> LoggingConfig:
    log_file = data.get("file", "logs/crawler.log")
    return LoggingConfig(
        level=str(data.get("level", "INFO")).upper(),
        file=_resolve_relative_path(str(log_file), config_dir) if log_file else None,
        console=bool(data.get("console", True)),
    )


def _load_retry_on(value: Any) -> tuple[str, ...]:
    items = tuple(str(v).lower() for v in _as_list(value or ["5xx", "timeout", "connection_error"], "retry_on"))
    unknown = sorted(set(items) - RETRY_KINDS)
    if unknown:
        raise ConfigError(f"retry_on has unsupported values: {', '.join(unknown)}")
    return items


def _expand_load_time(value: Any, config_dir: Path) -> Any:
    if isinstance(value, dict):
        return {k: _expand_load_time(v, config_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_load_time(v, config_dir) for v in value]
    if not isinstance(value, str):
        return value
    return _expand_file_refs(_expand_env(value), config_dir)


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"Missing environment variable: {name}")
        env_value = os.environ[name]
        if env_value == "":
            warnings.warn(f"Environment variable {name} is empty", RuntimeWarning)
        return env_value

    return _ENV_RE.sub(replace, value)


def _expand_file_refs(value: str, config_dir: Path) -> str:
    if value.startswith("file:"):
        return _read_secret_file(value[5:], config_dir)

    def replace(match: re.Match[str]) -> str:
        return _read_secret_file(match.group(1), config_dir)

    return _FILE_RE.sub(replace, value)


def _read_secret_file(path_text: str, config_dir: Path) -> str:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Unable to read file secret {path}: {exc}") from exc


def _validate_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _validate_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            _validate_placeholders(item)
    elif isinstance(value, str):
        unknown = sorted(set(_PLACEHOLDER_RE.findall(value)) - ALLOWED_PLACEHOLDERS)
        if unknown:
            raise ConfigError(f"Unknown placeholder(s): {', '.join(unknown)}")


def _resolve_relative_path(path_text: str, config_dir: Path) -> str:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return str(path)


def _normalize_exclude_dir(path_text: str) -> str:
    path = Path(path_text).expanduser()
    return str(path.resolve()) if path.is_absolute() else path_text.lower()


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list")
    return value


def _as_str_tuple(value: Any, label: str) -> tuple[str, ...]:
    return tuple(str(v) for v in _as_list(value, label))


class _StrictFormat(dict):
    def __missing__(self, key: str) -> str:
        raise ConfigError(f"Missing runtime placeholder: {key}")

