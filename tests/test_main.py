from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import yaml

from doc_crawler.__main__ import EXIT_CONFIG_ERROR, EXIT_OK, EXIT_SERVER_UNREACHABLE, main, run
from doc_crawler.api import TransientError
from doc_crawler.config import load_config
from doc_crawler.crawler import FoundFile
from test_support import temp_dir


def write_config(root: Path) -> Path:
    data = {
        "crawl": {"directories": [str(root)], "extensions": [".pdf"]},
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
        "retry": {"attempts": 1, "backoff_seconds": 0},
        "logging": {"console": False, "file": None},
        "cache": {"enabled": False},
    }
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class MainTests(unittest.TestCase):
    def test_run_orchestrates_exists_missing_and_locked(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(write_config(root))
            files = [
                FoundFile(str(root / "exists.pdf"), 1, 1.0),
                FoundFile(str(root / "missing.pdf"), 1, 1.0),
                FoundFile(str(root / "locked.pdf"), 1, 1.0),
            ]

            def fake_hash(path, _algo):
                if str(path).endswith("locked.pdf"):
                    raise PermissionError("locked")
                return "digest"

            with mock.patch("doc_crawler.__main__.iter_files", return_value=iter(files)), \
                mock.patch("doc_crawler.__main__.hash_file", side_effect=fake_hash), \
                mock.patch("doc_crawler.__main__.check_exists", side_effect=["exists", "missing"]), \
                mock.patch("doc_crawler.__main__.upload", return_value=True):
                stats = run(cfg)

            self.assertEqual(stats.processed, 3)
            self.assertEqual(stats.exists, 1)
            self.assertEqual(stats.uploaded, 1)
            self.assertEqual(stats.skipped_locked, 1)
            self.assertEqual(stats.errors, 1)

    def test_server_unreachable_maps_to_exit_3(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg_path = write_config(root)
            files = [FoundFile(str(root / "a.pdf"), 1, 1.0)]

            with mock.patch("doc_crawler.__main__.iter_files", return_value=iter(files)), \
                mock.patch("doc_crawler.__main__.hash_file", return_value="digest"), \
                mock.patch("doc_crawler.__main__.check_exists", side_effect=TransientError("down", "connection_error")):
                self.assertEqual(main(["--config", str(cfg_path)]), EXIT_SERVER_UNREACHABLE)

    def test_bad_config_maps_to_exit_2(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("crawl: []\n", encoding="utf-8")

            self.assertEqual(main(["--config", str(path)]), EXIT_CONFIG_ERROR)

    def test_dry_run_and_limit_are_honored(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(write_config(root))
            files = [
                FoundFile(str(root / "a.pdf"), 1, 1.0),
                FoundFile(str(root / "b.pdf"), 1, 1.0),
            ]

            with mock.patch("doc_crawler.__main__.iter_files", return_value=iter(files)), \
                mock.patch("doc_crawler.__main__.hash_file", return_value="digest"), \
                mock.patch("doc_crawler.__main__.check_exists") as check, \
                mock.patch("doc_crawler.__main__.upload") as upload:
                stats = run(cfg, dry_run=True, limit=1)

            self.assertEqual(stats.processed, 1)
            self.assertEqual(stats.skipped, 1)
            check.assert_not_called()
            upload.assert_not_called()

    def test_upload_false_logs_rejected_and_counts_error(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg = load_config(write_config(root))
            files = [FoundFile(str(root / "rejected.pdf"), 1, 1.0)]

            with mock.patch("doc_crawler.__main__.iter_files", return_value=iter(files)), \
                mock.patch("doc_crawler.__main__.hash_file", return_value="digest"), \
                mock.patch("doc_crawler.__main__.check_exists", return_value="missing"), \
                mock.patch("doc_crawler.__main__.upload", return_value=False), \
                self.assertLogs("doc_crawler.__main__", level="INFO") as logs:
                stats = run(cfg)

            self.assertEqual(stats.uploaded, 0)
            self.assertEqual(stats.errors, 1)
            self.assertIn("file_rejected", "\n".join(logs.output))

    def test_clean_run_exit_zero(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            cfg_path = write_config(root)

            with mock.patch("doc_crawler.__main__.run") as fake_run:
                fake_run.return_value.errors = 0
                self.assertEqual(main(["--config", str(cfg_path)]), EXIT_OK)


if __name__ == "__main__":
    unittest.main()
