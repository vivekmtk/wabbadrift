"""Wabbajack-compatible file hashes: base64 of the little-endian xxHash64 (seed 0)."""

from __future__ import annotations

import base64
import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import xxhash

_CHUNK = 1 << 20


def encode(digest: int) -> str:
    return base64.b64encode(struct.pack("<Q", digest)).decode("ascii")


def hash_bytes(data: bytes) -> str:
    return encode(xxhash.xxh64_intdigest(data))


def hash_file(path: str | Path) -> str:
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return encode(h.intdigest())


class HashCache:
    """Content hashes cached by absolute path + size + mtime, in a SQLite file."""

    def __init__(self, db_path: str | Path | None, workers: int = 4):
        self.workers = workers
        self.db = None
        self.mem: dict[str, tuple[int, int, str]] = {}
        if db_path is not None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # the server uses one cache from several request threads, one at a time (pair lock)
            self.db = sqlite3.connect(str(db_path), check_same_thread=False)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS hashes "
                "(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, hash TEXT)"
            )
            for path, size, mtime, h in self.db.execute("SELECT * FROM hashes"):
                self.mem[path] = (size, mtime, h)

    @staticmethod
    def _key(path: str | Path) -> str:
        return str(path).lower()

    def get(self, path: str | Path, size: int, mtime_ns: int) -> str | None:
        hit = self.mem.get(self._key(path))
        if hit and hit[0] == size and hit[1] == mtime_ns:
            return hit[2]
        return None

    def hash_many(self, files: list[tuple[str, int, int]]) -> dict[str, str]:
        """Hash ``(path, size, mtime_ns)`` entries, using the cache; returns path -> hash."""
        out: dict[str, str] = {}
        todo = []
        for path, size, mtime in files:
            h = self.get(path, size, mtime)
            if h is None:
                todo.append((path, size, mtime))
            else:
                out[path] = h
        if not todo:
            return out
        with ThreadPoolExecutor(self.workers) as pool:
            hashes = list(pool.map(lambda f: hash_file(f[0]), todo))
        rows = []
        for (path, size, mtime), h in zip(todo, hashes):
            out[path] = h
            self.mem[self._key(path)] = (size, mtime, h)
            rows.append((self._key(path), size, mtime, h))
        if self.db is not None:
            self.db.executemany(
                "INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?)", rows
            )
            self.db.commit()
        return out

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
