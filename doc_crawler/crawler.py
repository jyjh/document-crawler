from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import CrawlConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoundFile:
    path: str
    size: int
    mtime: float


def iter_files(crawl: CrawlConfig) -> Iterator[FoundFile]:
    extensions = {ext.lower() for ext in crawl.extensions}
    max_size = crawl.max_file_size_bytes

    for root_text in crawl.directories:
        root = Path(root_text).expanduser()
        if not root.exists():
            logger.warning("crawl_directory_missing path=%s", root)
            continue

        if crawl.recursive:
            yield from _iter_recursive(root, crawl, extensions, max_size)
        else:
            yield from _iter_single_dir(root, crawl, extensions, max_size)


def hash_file(path: str | os.PathLike[str], algo: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _iter_recursive(
    root: Path,
    crawl: CrawlConfig,
    extensions: set[str],
    max_size: int | None,
) -> Iterator[FoundFile]:
    def onerror(exc: OSError) -> None:
        logger.warning("walk_error path=%s error=%s", getattr(exc, "filename", root), exc)

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames if not _is_excluded(current / name, crawl.exclude_dirs)
        ]
        for filename in filenames:
            found = _candidate(current / filename, extensions, max_size)
            if found is not None:
                yield found


def _iter_single_dir(
    root: Path,
    crawl: CrawlConfig,
    extensions: set[str],
    max_size: int | None,
) -> Iterator[FoundFile]:
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                found = _candidate(Path(entry.path), extensions, max_size)
                if found is not None:
                    yield found
    except OSError as exc:
        logger.warning("walk_error path=%s error=%s", root, exc)


def _candidate(path: Path, extensions: set[str], max_size: int | None) -> FoundFile | None:
    if path.suffix.lower() not in extensions:
        return None
    try:
        stat = path.stat()
    except (PermissionError, OSError) as exc:
        logger.warning("stat_failed path=%s error=%s", path, exc)
        return None
    if max_size is not None and stat.st_size > max_size:
        logger.info("skipped_too_large path=%s size=%s max=%s", path, stat.st_size, max_size)
        return None
    return FoundFile(path=str(path), size=stat.st_size, mtime=stat.st_mtime)


def _is_excluded(path: Path, excludes: tuple[str, ...]) -> bool:
    if not excludes:
        return False

    name = path.name.lower()
    try:
        resolved = str(path.resolve()).lower()
    except OSError:
        resolved = str(path.absolute()).lower()

    for exclude in excludes:
        exclude_text = exclude.lower()
        if os.path.isabs(exclude_text):
            if resolved == exclude_text:
                return True
        elif name == exclude_text:
            return True
    return False

