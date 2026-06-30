from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest import mock

from doc_crawler.config import CrawlConfig, DirectorySearch
from doc_crawler.crawler import hash_file, iter_files
from test_support import temp_dir


class CrawlerTests(unittest.TestCase):
    def test_iter_files_filters_extensions_excludes_and_size(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            keep = root / "keep.pdf"
            keep.write_bytes(b"pdf")
            (root / "note.txt").write_text("no", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            nested_pdf = nested / "nested.PDF"
            nested_pdf.write_bytes(b"pdf")
            excluded = root / "skip"
            excluded.mkdir()
            (excluded / "hidden.pdf").write_bytes(b"pdf")
            large = root / "large.pdf"
            large.write_bytes(b"x" * 20)

            crawl = CrawlConfig(
                directories=(str(root),),
                extensions=(".pdf",),
                recursive=True,
                exclude_dirs=("skip",),
                max_file_size_mb=0.00001,
            )

            paths = {Path(item.path).name for item in iter_files(crawl)}

            self.assertEqual(paths, {"keep.pdf", "nested.PDF"})

    def test_non_recursive_only_reads_top_level(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            (root / "top.pdf").write_bytes(b"top")
            nested = root / "nested"
            nested.mkdir()
            (nested / "nested.pdf").write_bytes(b"nested")

            crawl = CrawlConfig(directories=(str(root),), extensions=(".pdf",), recursive=False)

            self.assertEqual([Path(item.path).name for item in iter_files(crawl)], ["top.pdf"])

    def test_directory_search_scans_matched_folders_recursively(self) -> None:
        import re

        with temp_dir() as tmp:
            parent = Path(tmp) / "library"
            parent.mkdir()
            (parent / "case-001").mkdir()
            (parent / "case-001" / "a.pdf").write_bytes(b"a")
            (parent / "case-001" / "sub").mkdir()
            (parent / "case-001" / "sub" / "b.pdf").write_bytes(b"b")
            (parent / "misc").mkdir()
            (parent / "misc" / "ignore.pdf").write_bytes(b"i")
            (parent / "case-002").mkdir()
            (parent / "case-002" / "c.pdf").write_bytes(b"c")

            crawl = CrawlConfig(
                directories=(),
                extensions=(".pdf",),
                recursive=True,
                directory_search=DirectorySearch(
                    parent=str(parent),
                    pattern=re.compile(r"case-\d+"),
                ),
            )

            found = sorted(Path(item.path).name for item in iter_files(crawl))
            self.assertEqual(found, ["a.pdf", "b.pdf", "c.pdf"])

    def test_directory_search_non_recursive_only_matches_top_level(self) -> None:
        import re

        with temp_dir() as tmp:
            parent = Path(tmp) / "library"
            parent.mkdir()
            (parent / "case-001").mkdir()
            (parent / "case-001" / "a.pdf").write_bytes(b"a")
            # A matching folder nested deeper should be ignored when
            # directory_search.recursive is False.
            (parent / "holder").mkdir()
            (parent / "holder" / "case-002").mkdir()
            (parent / "holder" / "case-002" / "c.pdf").write_bytes(b"c")

            crawl = CrawlConfig(
                directories=(),
                extensions=(".pdf",),
                recursive=True,
                directory_search=DirectorySearch(
                    parent=str(parent),
                    pattern=re.compile(r"case-\d+"),
                    recursive=False,
                ),
            )

            found = sorted(Path(item.path).name for item in iter_files(crawl))
            self.assertEqual(found, ["a.pdf"])

    def test_hash_file_streams_expected_digest(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "a.pdf"
            payload = b"abcdef" * 100
            path.write_bytes(payload)

            self.assertEqual(hash_file(path, "sha256", chunk=7), hashlib.sha256(payload).hexdigest())

    def test_hash_file_propagates_permission_error(self) -> None:
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                hash_file("locked.pdf", "sha256")


if __name__ == "__main__":
    unittest.main()
