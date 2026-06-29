from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from doc_crawler.api import ApiError, UnknownResponseError, check_exists, make_file_context, make_session, upload
from doc_crawler.config import load_config
from doc_crawler.crawler import FoundFile
from test_support import temp_dir


def response(status: int, body: bytes = b"") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    return resp


def write_config(root: Path, overrides: dict | None = None) -> Path:
    data = {
        "crawl": {"directories": [str(root)], "extensions": [".pdf"]},
        "server": {
            "base_url": "https://example.test",
            "headers": {"Authorization": "Bearer token"},
            "timeout_seconds": 5,
        },
        "check": {
            "method": "GET",
            "url": "{base_url}/exists/{filename}",
            "query": {"hash": "{hash}", "path": "{filepath}"},
            "detect": {"mode": "status_map", "status_map": {"exists": [200], "missing": [404]}},
            "on_unknown": "error",
            "retry_on": ["5xx", "timeout", "connection_error"],
        },
        "upload": {
            "method": "POST",
            "url": "{base_url}/upload",
            "format": "multipart",
            "file_field": "file",
            "filename_template": "{filename}",
            "extra_fields": {"hash": "{hash}", "filename": "{filename}"},
            "detect": {"mode": "status_in", "status_in": [201]},
            "idempotent": False,
            "retry_on": ["5xx", "timeout", "connection_error"],
        },
        "retry": {"attempts": 2, "backoff_seconds": 0},
        "logging": {"console": False, "file": None},
    }
    if overrides:
        deep_update(data, overrides)
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def deep_update(target: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ApiTests(unittest.TestCase):
    def test_status_map_check_and_placeholder_rendering(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            file_path = root / "white paper \u00f8.pdf"
            file_path.write_bytes(b"pdf")
            cfg = load_config(write_config(root))
            found = FoundFile(str(file_path), size=3, mtime=1.0)
            ctx = make_file_context(cfg.server.base_url, found, "abc")
            session = FakeSession(response(404))

            self.assertEqual(check_exists(cfg, session, ctx), "missing")
            method, url, kwargs = session.calls[0]
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://example.test/exists/white paper \u00f8.pdf")
            self.assertEqual(kwargs["params"]["hash"], "abc")
            self.assertEqual(kwargs["params"]["path"], str(file_path))

    def test_json_path_check_modes_and_unknown_policy(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(
                write_config(
                    root,
                    {
                        "check": {
                            "detect": {"mode": "json_path", "json_path": "data.exists"},
                            "on_unknown": "treat_missing",
                        }
                    },
                )
            )
            ctx = make_file_context(cfg.server.base_url, FoundFile(str(root / "a.pdf"), 1, 1.0), "abc")

            self.assertEqual(check_exists(cfg, FakeSession(response(200, b'{"data":{"exists":true}}')), ctx), "exists")
            self.assertEqual(check_exists(cfg, FakeSession(response(200, b'{"data":{}}')), ctx), "missing")

            cfg_skip = load_config(
                write_config(
                    root,
                    {
                        "check": {
                            "detect": {"mode": "json_path", "json_path": "missing"},
                            "on_unknown": "skip",
                        }
                    },
                )
            )
            self.assertEqual(check_exists(cfg_skip, FakeSession(response(200, b"{}")), ctx), "skip")

    def test_uploads_check_hash_response_shapes(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(
                write_config(
                    root,
                    {
                        "server": {"base_url": "http://100.87.142.5:8000"},
                        "check": {
                            "method": "GET",
                            "url": "{base_url}/api/uploads/check-hash",
                            "query": {"hash": "{hash}"},
                            "detect": {
                                "mode": "json_path",
                                "json_path": "exists",
                                "json_path_truthy": True,
                            },
                        },
                    },
                )
            )
            ctx = make_file_context(
                cfg.server.base_url,
                FoundFile(str(root / "Contents_Round9.pdf"), 1, 1.0),
                "2775d1fab0052875d9585595366b5c07fb84c294e1f0f486013e33c3ad49bf4e",
            )
            duplicate = (
                b'{"hash":"2775d1fab0052875d9585595366b5c07fb84c294e1f0f486013e33c3ad49bf4e",'
                b'"exists":true,"duplicates":[{"filename":"","hash":"2775d1fab0052875d9585595366b5c07fb84c294e1f0f486013e33c3ad49bf4e",'
                b'"existing_filename":"Contents_Round9.pdf","status":"indexed","job_id":"101e8b017fc04ad280818081a6ace164"}]}'
            )
            non_duplicate = (
                b'{"hash":"2775d1fab0052875d9585595366b5c07fb84c294e1f0f486013e33c3ad49bf4a",'
                b'"exists":false,"duplicates":[]}'
            )
            invalid_hash = b'{"detail":"hash must be a 64-character SHA-256 hex string."}'

            duplicate_session = FakeSession(response(200, duplicate))
            self.assertEqual(check_exists(cfg, duplicate_session, ctx), "exists")
            self.assertEqual(
                duplicate_session.calls[0][1],
                "http://100.87.142.5:8000/api/uploads/check-hash",
            )
            self.assertEqual(duplicate_session.calls[0][2]["params"]["hash"], ctx["hash"])
            self.assertEqual(check_exists(cfg, FakeSession(response(200, non_duplicate)), ctx), "missing")
            with self.assertRaises(UnknownResponseError):
                check_exists(cfg, FakeSession(response(422, invalid_hash)), ctx)

    def test_unknown_check_raises_when_policy_error(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(write_config(root))
            ctx = make_file_context(cfg.server.base_url, FoundFile(str(root / "a.pdf"), 1, 1.0), "abc")

            with self.assertRaises(UnknownResponseError):
                check_exists(cfg, FakeSession(response(418)), ctx)

    def test_multipart_upload_shape(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            file_path = root / "a.pdf"
            file_path.write_bytes(b"pdf")
            cfg = load_config(write_config(root))
            ctx = make_file_context(cfg.server.base_url, FoundFile(str(file_path), 3, 1.0), "abc")
            session = FakeSession(response(201))

            self.assertTrue(upload(cfg, session, ctx, str(file_path)))
            _method, url, kwargs = session.calls[0]
            self.assertEqual(url, "https://example.test/upload")
            self.assertEqual(kwargs["data"]["hash"], "abc")
            self.assertEqual(kwargs["files"]["file"][0], "a.pdf")

    def test_raw_and_json_upload_shapes(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            file_path = root / "a.pdf"
            file_path.write_bytes(b"pdf")
            ctx_file = FoundFile(str(file_path), 3, 1.0)

            raw_cfg = load_config(write_config(root, {"upload": {"format": "raw"}}))
            raw_ctx = make_file_context(raw_cfg.server.base_url, ctx_file, "abc")
            raw_session = FakeSession(response(201))
            self.assertTrue(upload(raw_cfg, raw_session, raw_ctx, str(file_path)))
            self.assertIn("data", raw_session.calls[0][2])

            json_cfg = load_config(write_config(root, {"upload": {"format": "json"}}))
            json_ctx = make_file_context(json_cfg.server.base_url, ctx_file, "abc")
            json_session = FakeSession(response(201))
            self.assertTrue(upload(json_cfg, json_session, json_ctx, str(file_path)))
            self.assertEqual(json_session.calls[0][2]["json"]["hash"], "abc")

    def test_retries_timeout_and_does_not_retry_non_idempotent_upload_drop(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            file_path = root / "a.pdf"
            file_path.write_bytes(b"pdf")
            cfg = load_config(write_config(root))
            ctx = make_file_context(cfg.server.base_url, FoundFile(str(file_path), 3, 1.0), "abc")

            check_session = FakeSession(requests.exceptions.Timeout(), response(200))
            with mock.patch("time.sleep"):
                self.assertEqual(check_exists(cfg, check_session, ctx), "exists")
            self.assertEqual(len(check_session.calls), 2)

            upload_session = FakeSession(requests.exceptions.ConnectionError())
            with self.assertRaises(ApiError):
                upload(cfg, upload_session, ctx, str(file_path))
            self.assertEqual(len(upload_session.calls), 1)

    def test_make_session_applies_headers(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(write_config(root))

            session = make_session(cfg.server)

            self.assertEqual(session.headers["Authorization"], "Bearer token")


if __name__ == "__main__":
    unittest.main()
