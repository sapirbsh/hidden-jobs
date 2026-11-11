"""Tiny SQLite key/value cache with TTL.

Saves money: Google Places results and career-page detection are cached,
so re-running a search for the same city costs (almost) nothing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (ns TEXT, k TEXT, v TEXT, ts REAL, PRIMARY KEY (ns, k))"
        )
        self._conn.commit()

    def get(self, ns: str, key: str, max_age: float | None = None) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT v, ts FROM kv WHERE ns=? AND k=?", (ns, key)).fetchone()
        if not row:
            return None
        value, ts = row
        if max_age is not None and time.time() - ts > max_age:
            return None
        return json.loads(value)

    def set(self, ns: str, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (ns, k, v, ts) VALUES (?, ?, ?, ?)",
                (ns, key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._conn.commit()


DAY = 24 * 3600
