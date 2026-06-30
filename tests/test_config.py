from __future__ import annotations

import os
import unittest
import warnings
from pathlib import Path
from unittest import mock

import yaml

from doc_crawler.config import ConfigError, load_config
from test_support import temp_dir


def write_config(root: Path, data: dict) -> Path:
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def base_config(root: Path) -> dict:
    return {
        "crawl": {"directories": [str(root)], "extensions": ["pdf"]},
        "server": {"base_url": "https://example.test", "headers": {}},
        "check": {
            "method": "GET",
            "url": "{base_url}/exists",
            "query": {"hash": "{hash}"},
            "detect": {"mode": "status_map", "status_map": {"exists": [200], "missing": [404]}},
        },
        "upload": {
            "method": "POST",
            "url": "{base_url}/upload",
            "format": "multipart",
            "file_field": "file",
            "filename_template": "{filename}",
            "extra_fields": {"hash": "{hash}"},
            "detect": {"mode": "status_in", "status_in": [201]},
        },
        "cache": {"path": "cache.sqlite"},
        "logging": {"file": "logs/crawler.log", "console": False},
    }


class ConfigTests(unittest.TestCase):
    def test_env_and_file_expansion(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            secret = root / "token.txt"
            secret.write_text("file-token\n", encoding="utf-8")
            data = base_config(root)
            data["server"]["headers"] = {
                "Authorization": "Bearer ${API_TOKEN}",
                "X-File": f"Bearer file:{secret}",
            }
            path = write_config(root, data)

            with mock.patch.dict(os.environ, {"API_TOKEN": "env-token"}, clear=False):
                cfg = load_config(path)

            self.assertEqual(cfg.server.headers["Authorization"], "Bearer env-token")
            self.assertEqual(cfg.server.headers["X-File"], "Bearer file-token")

    def test_missing_env_is_error_and_empty_env_warns(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            data = base_config(root)
            data["server"]["headers"] = {"Authorization": "Bearer ${API_TOKEN}"}
            path = write_config(root, data)

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ConfigError):
                    load_config(path)

            with mock.patch.dict(os.environ, {"API_TOKEN": ""}, clear=True):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    cfg = load_config(path)

            self.assertEqual(cfg.server.headers["Authorization"], "Bearer ")
            self.assertTrue(any("empty" in str(item.message) for item in caught))

    def test_query_and_json_body_are_mutually_exclusive(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            data = base_config(root)
            data["check"]["json_body"] = {"hash": "{hash}"}
            path = write_config(root, data)

            with self.assertRaisesRegex(ConfigError, "mutually exclusive"):
                load_config(path)

    def test_extensions_and_relative_paths_are_normalized(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            data = base_config(root)
            data["crawl"]["extensions"] = ["PDF", ".txt"]
            path = write_config(root, data)

            cfg = load_config(path)

            self.assertEqual(cfg.crawl.extensions, (".pdf", ".txt"))
            self.assertEqual(cfg.cache.path, str(root / "cache.sqlite"))
            self.assertEqual(cfg.logging.file, str(root / "logs" / "crawler.log"))

    def test_unknown_placeholder_raises(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            data = base_config(root)
            data["check"]["url"] = "{base_url}/exists/{unknown}"
            path = write_config(root, data)

            with self.assertRaisesRegex(ConfigError, "Unknown placeholder"):
                load_config(path)

    def test_directory_search_loads_compiled_pattern(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            parent = root / "library"
            parent.mkdir()
            data = base_config(root)
            data["crawl"]["directory_search"] = {
                "parent": str(parent),
                "pattern": r"case-\d+",
            }
            path = write_config(root, data)

            cfg = load_config(path)

            self.assertIsNotNone(cfg.crawl.directory_search)
            assert cfg.crawl.directory_search is not None
            self.assertEqual(cfg.crawl.directory_search.parent, str(parent))
            self.assertEqual(cfg.crawl.directory_search.pattern.pattern, r"case-\d+")
            self.assertTrue(cfg.crawl.directory_search.recursive)

    def test_directory_search_invalid_regex_raises(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            data = base_config(root)
            data["crawl"]["directory_search"] = {
                "parent": str(root),
                "pattern": r"([unclosed",
            }
            path = write_config(root, data)

            with self.assertRaisesRegex(ConfigError, "not a valid regex"):
                load_config(path)

    def test_directory_search_missing_fields_raise(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root, base_config(root) | {})

            # No parent
            data = base_config(root)
            data["crawl"]["directory_search"] = {"pattern": r"x"}
            with self.assertRaisesRegex(ConfigError, "parent is required"):
                load_config(write_config(root, data))

            # No pattern
            data = base_config(root)
            data["crawl"]["directory_search"] = {"parent": str(root)}
            with self.assertRaisesRegex(ConfigError, "pattern is required"):
                load_config(write_config(root, data))


if __name__ == "__main__":
    unittest.main()
