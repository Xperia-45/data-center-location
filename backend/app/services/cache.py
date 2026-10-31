"""
Stage 3A — generic namespaced cache (bounded LRU + best-effort SQLite + single-flight).

Cache keys are namespaced and always include the site coordinates, the analysis radius
and the resolution/layer selection, so one site or one resolution can never serve
another site's geography.

A namespace is VERSIONED (e.g. `landscape:v2`): bump it whenever the payload schema
changes, so entries written by older code become unreachable. An optional `validator`
adds a second guarantee at read time - a payload that fails it is treated as a cache
miss and evicted, so a stale payload can never be served even if an old row survives.

`elevation.py` keeps its own dedicated cache stack (with Open-Meteo specific rate-limit
cooldown state) and applies the same rule: its namespace is versioned (`dem:v2`) and its
read paths refuse a payload that predates the current provenance fields.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import sqlite3
from collections import OrderedDict
from collections.abc import Callable

from ..db import conn

ALLOWED_TABLES = ("landscape_cache", "elevation_cache")


def stable_digest(*parts: object) -> str:
    """Deterministic digest over JSON-serialisable parts (used for fingerprints/keys)."""
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_digest(*parts: object, length: int = 16) -> str:
    return stable_digest(*parts)[:length]


class NamespacedCache:
    """Bounded in-memory LRU backed by a SQLite table, with single-flight deduplication."""

    def __init__(
        self,
        namespace: str,
        table: str = "landscape_cache",
        max_entries: int = 64,
        validator: Callable[[dict], bool] | None = None,
    ):
        if table not in ALLOWED_TABLES:
            raise ValueError(f"cache table must be one of {ALLOWED_TABLES}")
        self.namespace = namespace
        self.table = table
        self.max_entries = max(1, int(max_entries))
        self.validator = validator
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._counts: dict[str, int] = {}

    # -- keys ---------------------------------------------------------------
    def key(self, **parts: object) -> str:
        ordered = ":".join(f"{k}={parts[k]}" for k in sorted(parts))
        return f"{self.namespace}:{ordered}"

    # -- schema guard -------------------------------------------------------
    def _accept(self, key: str, payload: dict) -> bool:
        """False (and evict) when a payload was written by an older, superseded schema."""
        if self.validator is None:
            return True
        try:
            accepted = bool(self.validator(payload))
        except Exception:  # pragma: no cover - a broken validator must fail closed
            accepted = False
        if not accepted:
            self._mem.pop(key, None)
        return accepted

    # -- memory tier --------------------------------------------------------
    def _mem_get(self, key: str) -> dict | None:
        item = self._mem.get(key)
        if item is None:
            return None
        if not self._accept(key, item):
            return None
        self._mem.move_to_end(key)
        out = dict(item)
        out["cache_hit"] = "memory"
        return out

    def _mem_put(self, key: str, payload: dict) -> None:
        self._mem[key] = dict(payload)
        self._mem.move_to_end(key)
        while len(self._mem) > self.max_entries:
            self._mem.popitem(last=False)

    # -- sqlite tier --------------------------------------------------------
    def _sql_get(self, key: str) -> dict | None:
        try:
            c = conn()
            row = c.execute(f"SELECT payload FROM {self.table} WHERE cache_key=?", (key,)).fetchone()
            c.close()
        except (sqlite3.Error, OSError):
            return None
        if not row:
            return None
        try:
            parsed = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if not self._accept(key, parsed):
            return None
        self._mem_put(key, parsed)
        out = dict(parsed)
        out["cache_hit"] = "sqlite"
        return out

    def _sql_put(self, key: str, payload: dict) -> None:
        try:
            c = conn()
            c.execute(
                f"INSERT OR REPLACE INTO {self.table}(cache_key, payload, created_at) VALUES (?,?,?)",
                (key, json.dumps(payload), datetime.datetime.utcnow().isoformat() + "Z"),
            )
            c.commit()
            c.close()
        except (sqlite3.Error, OSError):
            # Ephemeral / read-only storage degrades cleanly to the memory tier.
            pass

    # -- public API ---------------------------------------------------------
    def get(self, key: str) -> dict | None:
        return self._mem_get(key) or self._sql_get(key)

    def put(self, key: str, payload: dict, persist: bool = True) -> None:
        self._mem_put(key, payload)
        if persist:
            self._sql_put(key, payload)

    def clear(self, purge_sqlite: bool = True) -> None:
        self._mem.clear()
        self._locks.clear()
        self._counts.clear()
        if purge_sqlite:
            try:
                c = conn()
                c.execute(f"DELETE FROM {self.table}")
                c.commit()
                c.close()
            except (sqlite3.Error, OSError):
                pass

    async def get_or_fetch(self, key: str, factory, persist: bool = True) -> dict:
        """
        Returns a cached payload or builds it once. Concurrent callers for the same key
        share a single upstream build (single-flight) instead of stampeding the provider.
        """
        cached = self.get(key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        self._counts[key] = self._counts.get(key, 0) + 1
        try:
            async with lock:
                cached = self.get(key)
                if cached is not None:
                    return cached
                payload = await factory()
                if not isinstance(payload, dict):  # pragma: no cover - defensive
                    raise TypeError("cache factory must return a dict")
                payload.setdefault("cache_hit", None)
                # Never cache a degraded payload: a transient upstream failure must not be
                # frozen into either tier and served as geography on a later request.
                if payload.get("cacheable", True):
                    self.put(key, payload, persist=persist)
                return payload
        finally:
            remaining = self._counts.get(key, 1) - 1
            if remaining <= 0:
                self._counts.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._counts[key] = remaining
