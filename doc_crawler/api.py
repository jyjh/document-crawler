from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from .config import AppConfig, DetectConfig, render_template
from .crawler import FoundFile


logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    pass


class UnknownResponseError(ApiError):
    pass


class TransientError(ApiError):
    def __init__(self, message: str, kind: str, response: requests.Response | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.response = response


class ServerUnreachableError(ApiError):
    pass


@dataclass(frozen=True)
class Endpoint:
    method: str
    url: str
    detect: DetectConfig
    retry_on: tuple[str, ...]


def make_session(server) -> requests.Session:
    session = requests.Session()
    if server.headers:
        session.headers.update(server.headers)
    return session


def make_file_context(base_url: str, file: FoundFile, digest: str) -> dict[str, Any]:
    path = Path(file.path)
    return {
        "base_url": base_url.rstrip("/"),
        "hash": digest,
        "filename": path.name,
        "filepath": str(path),
        "filesize": file.size,
        "mtime_iso": datetime.fromtimestamp(file.mtime, tz=timezone.utc).isoformat(),
    }


def check_exists(cfg: AppConfig, session: requests.Session, file_ctx: Mapping[str, Any]) -> str:
    def once() -> str:
        url = render_template(cfg.check.url, file_ctx)
        query = render_template(cfg.check.query, file_ctx) if cfg.check.query else None
        json_body = render_template(cfg.check.json_body, file_ctx) if cfg.check.json_body else None
        response = _request(
            cfg,
            session,
            cfg.check.method,
            url,
            params=query,
            json=json_body,
            retry_connection_errors=True,
        )
        try:
            return _detect_check(response, cfg.check.detect)
        except UnknownResponseError:
            if cfg.check.on_unknown == "treat_missing":
                logger.warning("check_unknown_treated_missing status=%s", response.status_code)
                return "missing"
            if cfg.check.on_unknown == "skip":
                logger.warning("check_unknown_skipped status=%s", response.status_code)
                return "skip"
            raise

    return _with_retries(once, cfg.retry.attempts, cfg.retry.backoff_seconds, cfg.check.retry_on)


def upload(cfg: AppConfig, session: requests.Session, file_ctx: Mapping[str, Any], path: str) -> bool:
    def once() -> bool:
        url = render_template(cfg.upload.url, file_ctx)
        fields = render_template(cfg.upload.extra_fields, file_ctx)
        filename = render_template(cfg.upload.filename_template, file_ctx)

        if cfg.upload.format == "multipart":
            with open(path, "rb") as fh:
                files = {
                    cfg.upload.file_field: (filename, fh, "application/pdf"),
                }
                response = _request(
                    cfg,
                    session,
                    cfg.upload.method,
                    url,
                    data=fields,
                    files=files,
                    retry_connection_errors=cfg.upload.idempotent,
                )
        elif cfg.upload.format == "raw":
            with open(path, "rb") as fh:
                response = _request(
                    cfg,
                    session,
                    cfg.upload.method,
                    url,
                    data=fh,
                    retry_connection_errors=cfg.upload.idempotent,
                )
        else:
            response = _request(
                cfg,
                session,
                cfg.upload.method,
                url,
                json=fields,
                retry_connection_errors=True,
            )

        return _detect_upload(response, cfg.upload.detect)

    return _with_retries(once, cfg.retry.attempts, cfg.retry.backoff_seconds, cfg.upload.retry_on)


def _request(
    cfg: AppConfig,
    session: requests.Session,
    method: str,
    url: str,
    *,
    retry_connection_errors: bool,
    **kwargs: Any,
) -> requests.Response:
    try:
        response = session.request(
            method,
            url,
            timeout=cfg.server.timeout_seconds,
            verify=cfg.server.verify,
            **kwargs,
        )
    except requests.exceptions.Timeout as exc:
        if retry_connection_errors:
            raise TransientError(f"timeout requesting {url}", "timeout") from exc
        raise ApiError(f"request timeout during non-idempotent upload: {url}") from exc
    except requests.exceptions.ConnectionError as exc:
        if retry_connection_errors:
            raise TransientError(f"connection error requesting {url}", "connection_error") from exc
        raise ApiError(f"connection error during non-idempotent upload: {url}") from exc
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"request failed: {url}: {exc}") from exc

    if 500 <= response.status_code <= 599:
        raise TransientError(f"server returned {response.status_code}: {url}", "5xx", response)
    return response


def _detect_check(response: requests.Response, detect: DetectConfig) -> str:
    if detect.mode == "always_exists":
        return "exists"
    if detect.mode == "status_map":
        assert detect.status_map is not None
        if response.status_code in detect.status_map["exists"]:
            return "exists"
        if response.status_code in detect.status_map["missing"]:
            return "missing"
        raise UnknownResponseError(f"unexpected check status: {response.status_code}")
    if detect.mode == "json_path":
        value = _json_path_value(response, detect)
        return _json_value_to_exists(value, detect)
    raise UnknownResponseError(f"unsupported check detect mode: {detect.mode}")


def _detect_upload(response: requests.Response, detect: DetectConfig) -> bool:
    if detect.mode == "status_in":
        assert detect.status_in is not None
        if response.status_code in detect.status_in:
            return True
        raise UnknownResponseError(f"unexpected upload status: {response.status_code}")
    if detect.mode == "json_path":
        value = _json_path_value(response, detect)
        if detect.json_path_eq is not None:
            return value == detect.json_path_eq
        return bool(value) if detect.json_path_truthy else not bool(value)
    raise UnknownResponseError(f"unsupported upload detect mode: {detect.mode}")


def _json_path_value(response: requests.Response, detect: DetectConfig) -> Any:
    try:
        data = response.json()
    except ValueError as exc:
        raise UnknownResponseError("response is not JSON") from exc

    value: Any = data
    assert detect.json_path is not None
    for part in detect.json_path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise UnknownResponseError(f"missing JSON path: {detect.json_path}")
    return value


def _json_value_to_exists(value: Any, detect: DetectConfig) -> str:
    if detect.json_path_eq is not None:
        return "exists" if value == detect.json_path_eq else "missing"
    if detect.json_path_truthy:
        return "exists" if bool(value) else "missing"
    return "exists" if not bool(value) else "missing"


def _with_retries(
    fn: Callable[[], Any],
    attempts: int,
    backoff_seconds: float,
    retry_on: tuple[str, ...],
) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TransientError as exc:
            if exc.kind not in retry_on or attempt >= attempts:
                raise
            delay = backoff_seconds * (2 ** (attempt - 1))
            if delay:
                delay += random.uniform(0, min(0.25, delay * 0.1))
                time.sleep(delay)
            logger.warning(
                "request_retry attempt=%s attempts=%s kind=%s",
                attempt,
                attempts,
                exc.kind,
            )
    raise AssertionError("retry loop exited unexpectedly")

