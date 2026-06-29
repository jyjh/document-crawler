from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .crawler import FoundFile


logger = logging.getLogger(__name__)


class HashCache:
    def __init__(
        self,
        path: str,
        algorithm: str,
        *,
        enabled: bool = True,
        trust_size_mtime: bool = True,
    ) -> None:
        self.path = path
        self.algorithm = algorithm
        self.enabled = enabled
        self.trust_size_mtime = trust_size_mtime
        self._conn: sqlite3.Connection | None = None

    def get(self, file: FoundFile) -> str | None:
        if not self.enabled or not self.trust_size_mtime:
            return None
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT hash, mtime, size FROM files WHERE path = ? AND algo = ?",
                (file.path, self.algorithm),
            ).fetchone()
        except sqlite3.Error as exc:
            self._disable(f"cache_get_failed error={exc}")
            return None
        if row is None:
            return None
        digest, mtime, size = row
        if float(mtime) == float(file.mtime) and int(size) == int(file.size):
            return str(digest)
        return None

    def put(self, file: FoundFile, digest: str) -> None:
        if not self.enabled:
            return
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO files(path, algo, mtime, size, hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file.path, self.algorithm, float(file.mtime), int(file.size), digest),
            )
            conn.commit()
        except sqlite3.Error as exc:
            self._disable(f"cache_put_failed error={exc}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connect(self) -> sqlite3.Connection | None:
        if not self.enabled:
            return None
        if self._conn is not None:
            return self._conn
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT NOT NULL,
                    algo TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    hash TEXT NOT NULL,
                    PRIMARY KEY(path, algo)
                )
                """
            )
            conn.commit()
            self._conn = conn
        except (OSError, sqlite3.Error) as exc:
            self._disable(f"cache_open_failed path={self.path} error={exc}")
            return None
        return self._conn

    def _disable(self, message: str) -> None:
        logger.warning("%s; disabling cache", message)
        self.enabled = False
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class DisabledHashCache(HashCache):
    def __init__(self) -> None:
        super().__init__(":memory:", "sha256", enabled=False)

