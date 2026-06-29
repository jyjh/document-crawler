from __future__ import annotations

import unittest
from pathlib import Path

from doc_crawler.cache import HashCache
from doc_crawler.crawler import FoundFile
from test_support import temp_dir


class CacheTests(unittest.TestCase):
    def test_put_get_round_trip_and_invalidates_on_metadata_change(self) -> None:
        cache = HashCache(":memory:", "sha256")
        file = FoundFile("a.pdf", size=10, mtime=123.0)

        self.assertIsNone(cache.get(file))
        cache.put(file, "digest")
        self.assertEqual(cache.get(file), "digest")
        self.assertIsNone(cache.get(FoundFile("a.pdf", size=11, mtime=123.0)))
        self.assertIsNone(cache.get(FoundFile("a.pdf", size=10, mtime=124.0)))
        cache.close()

    def test_misses_on_path_algo_disabled_and_untrusted_metadata(self) -> None:
        cache = HashCache(":memory:", "sha256")
        cache.put(FoundFile("a.pdf", size=10, mtime=123.0), "digest")

        self.assertIsNone(cache.get(FoundFile("b.pdf", size=10, mtime=123.0)))
        cache.close()

        other_algo = HashCache(":memory:", "sha1")
        self.assertIsNone(other_algo.get(FoundFile("a.pdf", size=10, mtime=123.0)))
        other_algo.close()

        disabled = HashCache(":memory:", "sha256", enabled=False)
        self.assertIsNone(disabled.get(FoundFile("a.pdf", size=10, mtime=123.0)))

        untrusted = HashCache(":memory:", "sha256", trust_size_mtime=False)
        untrusted.put(FoundFile("a.pdf", size=10, mtime=123.0), "digest")
        self.assertIsNone(untrusted.get(FoundFile("a.pdf", size=10, mtime=123.0)))
        untrusted.close()

    def test_db_open_failure_degrades_to_disabled(self) -> None:
        with temp_dir() as tmp:
            cache = HashCache(str(Path(tmp)), "sha256")

            self.assertIsNone(cache.get(FoundFile("a.pdf", size=10, mtime=123.0)))
            self.assertFalse(cache.enabled)


if __name__ == "__main__":
    unittest.main()
