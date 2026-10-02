"""In-process rate limiting.

A token bucket per ``(name, key)`` pair.  Deliberately in-memory and
deliberately simple:

* The target is one server process on one box.  A shared Redis would be a new
  always-on dependency to keep alive and secure for the sake of counters.
* Losing the counters on restart is acceptable: restarting the server is not an
  attack an operator can be expected to hide from.
* The *hard* brute-force protection is the ``login_attempts`` table plus
  account lockout in the database, which survives restarts.  This module is
  the cheaper, first line that keeps a single runaway client from saturating
  the CPU.

Keying is by client address for anonymous endpoints and by ``user:profile``
for authenticated ones, so one noisy profile cannot lock out the rest of the
household.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_MAX_BUCKETS = 20_000          # hard ceiling on memory
_IDLE_EVICTION_SECONDS = 900


@dataclass(slots=True)
class _Bucket:
    tokens: float
    capacity: float
    refill_per_second: float
    updated_at: float

    def take(self, now: float, amount: float = 1.0) -> tuple[bool, float, float]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True, self.tokens, 0.0
        deficit = amount - self.tokens
        return False, self.tokens, deficit / self.refill_per_second


class RateLimiter:
    """Fixed-capacity token buckets keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._policies: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def configure(self, name: str, per_minute: int, *, burst: float | None = None) -> None:
        """Register (or update) the policy for ``name``.

        Policies are declared up front from :class:`RateLimitConfig`; anything
        not registered falls back to the permissive default.
        """
        capacity = float(burst if burst is not None else per_minute)
        refill = capacity / 60.0
        with self._lock:
            self._policies[name] = (capacity, refill)

    def _policy(self, name: str) -> tuple[float, float]:
        policy = self._policies.get(name)
        if policy:
            return policy
        return 60.0, 1.0

    def allow(self, name: str, key: str, *, amount: float = 1.0,
              now: float | None = None) -> tuple[bool, float, float]:
        """Consume ``amount`` from the bucket.  Returns the decision tuple."""
        now = time.monotonic() if now is None else now
        capacity, refill = self._policy(name)
        with self._lock:
            self._maybe_sweep(now)
            bucket_key = (name, key)
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                if len(self._buckets) >= _MAX_BUCKETS:
                    self._evict_locked(now)
                bucket = _Bucket(tokens=capacity, capacity=capacity,
                                 refill_per_second=refill, updated_at=now)
                self._buckets[bucket_key] = bucket
            else:
                # Pick up a changed policy without dropping the client's state.
                bucket.capacity = capacity
                bucket.refill_per_second = refill
            return bucket.take(now, amount)

    def peek(self, name: str, key: str, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get((name, key))
            if bucket is None:
                return 0.0
            elapsed = max(0.0, now - bucket.updated_at)
            return min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second)

    def reset(self, name: str | None = None, key: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._buckets.clear()
                return
            if key is None:
                for bucket_key in [k for k in self._buckets if k[0] == name]:
                    del self._buckets[bucket_key]
                return
            self._buckets.pop((name, key), None)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        cutoff = now - _IDLE_EVICTION_SECONDS
        stale = [k for k, b in self._buckets.items() if b.updated_at < cutoff]
        for key in stale:
            del self._buckets[key]

    def _evict_locked(self, now: float) -> None:
        """Over the ceiling: drop the least recently active buckets."""
        if len(self._buckets) < _MAX_BUCKETS:
            return
        ordered = sorted(self._buckets.items(), key=lambda kv: kv[1].updated_at)
        for key, _ in ordered[: len(self._buckets) // 4]:
            del self._buckets[key]

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for name, _ in self._buckets:
                counts[name] = counts.get(name, 0) + 1
            return counts


class AccountLockout:
    """Database-backed lockout, consulted before any password hashing happens.

    This is the part that survives a server restart, which matters because the
    cheapest brute-force defence is also the one an attacker would try to
    survive by waiting for a maintenance window.
    """

    def __init__(self, db, *, max_failures: int = 8, lockout_seconds: int = 300,
                 history_window_seconds: int = 900) -> None:
        self.db = db
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.history_window_seconds = history_window_seconds

    def recent_failures(self, username: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE username = ? AND successful = 0 "
            "AND created_at > datetime('now', ?)",
            (username.lower(), f"-{self.history_window_seconds} seconds"),
        )
        return int(row[0]) if row else 0

    def is_locked(self, username: str) -> tuple[bool, int]:
        """Return ``(locked, seconds_remaining)``."""
        since = self.db.scalar(
            "SELECT MAX(created_at) FROM login_attempts "
            "WHERE username = ? AND successful = 0",
            (username.lower(),),
        )
        if not since:
            return False, 0
        failures = self.recent_failures(username)
        if failures < self.max_failures:
            return False, 0
        row = self.db.query_one(
            "SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER) AS age "
            "FROM login_attempts WHERE username = ? ORDER BY id DESC LIMIT 1",
            (since, username.lower()),
        )
        age = int(row["age"]) if row and row["age"] is not None else 0
        if age >= self.lockout_seconds:
            return False, 0
        return True, self.lockout_seconds - age

    def record_failure(self, username: str, ip: str | None, reason: str = "bad_credentials") -> None:
        self.db.execute(
            "INSERT INTO login_attempts (username, ip_address, successful, reason) "
            "VALUES (?, ?, 0, ?)",
            (username.lower()[:64], ip, reason[:64]),
        )

    def record_success(self, username: str, ip: str | None) -> None:
        self.db.execute(
            "INSERT INTO login_attempts (username, ip_address, successful, reason) "
            "VALUES (?, ?, 1, 'ok')",
            (username.lower()[:64], ip),
        )

    def recent_ip_failures(self, ip: str, window_seconds: int = 900) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_address = ? AND successful = 0 "
            "AND created_at > datetime('now', ?)",
            (ip, f"-{window_seconds} seconds"),
        )
        return int(row[0]) if row else 0

    def prune(self, keep_days: int = 30) -> int:
        result = self.db.execute(
            "DELETE FROM login_attempts WHERE created_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        return result.changes