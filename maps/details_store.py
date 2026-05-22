"""
========================================================
  SHARED STORE FOR PASS 2 — SQLite + WAL
  ─────────────────────────────────────────────────────
  Multi-process safe. Designed so you can launch N copies
  of pass2.py against the same DB without coordinating.

  Safety guarantees:
    • Atomic claim:  UPDATE … WHERE url = (SELECT … LIMIT 1)
                     in WAL mode is serialized by SQLite —
                     no two workers can grab the same row.
    • Lease timeout: an 'in_progress' row whose lease has
                     expired is reclaimable (handles crashes).
    • Bounded retry: attempts counter; rows past MAX_ATTEMPTS
                     are left in 'failed' and skipped.
    • Atomic save:   status flip + payload write happen in
                     one transaction.
    • Crash-safe:    WAL + synchronous=NORMAL survives kill -9.
========================================================
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


# ─── CONFIG ──────────────────────────────────────────────────────────────────

DB_PATH        = "places.db"
LEASE_SECONDS  = 5 * 60     # how long a claim is valid before another worker can steal it
MAX_ATTEMPTS   = 3          # give up on a URL after this many failed tries
HEARTBEAT_EVERY = 60        # workers should call refresh_lease() at least this often


# ─── SCHEMA ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    url               TEXT PRIMARY KEY,
    name              TEXT,
    keyword           TEXT,
    source_url        TEXT,
    discovered_at     TEXT,

    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|completed|failed
    worker_id         TEXT,
    lease_expires_at  REAL,                              -- unix ts
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    last_attempted_at TEXT,
    completed_at      TEXT,

    details_json      TEXT,                              -- extracted fields
    html_path         TEXT                               -- saved raw HTML, if any
);

CREATE INDEX IF NOT EXISTS idx_places_status
    ON places(status, lease_expires_at);
"""


def worker_id() -> str:
    """Stable per-process id: hostname-pid-shortuuid."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


# ─── STORE ───────────────────────────────────────────────────────────────────

class DetailsStore:
    def __init__(self, db_path: str = DB_PATH):
        self.path = Path(db_path)
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,            # wait up to 30s if another writer holds the lock
            isolation_level=None,    # autocommit; we manage transactions explicitly
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        c = self._conn
        c.execute("PRAGMA journal_mode = WAL;")        # concurrent readers + 1 writer
        c.execute("PRAGMA synchronous = NORMAL;")      # crash-safe, faster than FULL
        c.execute("PRAGMA busy_timeout = 30000;")      # 30s before SQLITE_BUSY
        c.execute("PRAGMA foreign_keys = ON;")

    def _migrate(self) -> None:
        self._conn.executescript(SCHEMA)

    # ─── INGEST FROM urls.json (idempotent) ─────────────────────────────────

    def import_from_urls_json(self, json_path: str) -> int:
        """
        Pull new rows from urls.json into the DB. Existing rows are NOT touched —
        you can re-run this any time pass 1 has discovered more URLs.
        """
        p = Path(json_path)
        if not p.exists():
            return 0
        with p.open() as f:
            data = json.load(f)

        added = 0
        with self._tx() as cur:
            for url, row in data.items():
                # INSERT OR IGNORE: leaves any existing row (and its status) alone
                res = cur.execute(
                    """
                    INSERT OR IGNORE INTO places
                        (url, name, keyword, source_url, discovered_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        url,
                        row.get("name"),
                        row.get("keyword"),
                        row.get("source_url"),
                        row.get("discovered_at"),
                    ),
                )
                if res.rowcount:
                    added += 1
        return added

    # ─── CLAIM / RELEASE ────────────────────────────────────────────────────

    def claim_next(self, wid: str,
                   lease_seconds: int = LEASE_SECONDS,
                   max_attempts: int = MAX_ATTEMPTS) -> Optional[sqlite3.Row]:
        """
        Atomically pick the next URL to process and lease it to `wid`.

        Eligible rows (priority order):
          1. status = 'pending'
          2. status = 'in_progress' with expired lease  (crashed worker)
          3. status = 'failed'      with attempts < max  (retryable error)

        Returns the row, or None if nothing is available.
        """
        now = time.time()
        new_expires = now + lease_seconds

        with self._tx() as cur:
            row = cur.execute(
                """
                UPDATE places
                   SET status            = 'in_progress',
                       worker_id         = ?,
                       lease_expires_at  = ?,
                       attempts          = attempts + 1,
                       last_attempted_at = datetime('now')
                 WHERE url = (
                     SELECT url FROM places
                      WHERE status = 'pending'
                         OR (status = 'in_progress' AND lease_expires_at < ?)
                         OR (status = 'failed'      AND attempts < ?)
                   ORDER BY
                       CASE status
                           WHEN 'pending'     THEN 0
                           WHEN 'in_progress' THEN 1
                           ELSE 2
                       END,
                       attempts ASC
                      LIMIT 1
                 )
                RETURNING *;
                """,
                (wid, new_expires, now, max_attempts),
            ).fetchone()
            return row

    def refresh_lease(self, url: str, wid: str,
                      lease_seconds: int = LEASE_SECONDS) -> bool:
        """Heartbeat — extends our claim. Returns False if we no longer own it."""
        new_expires = time.time() + lease_seconds
        with self._tx() as cur:
            res = cur.execute(
                """
                UPDATE places
                   SET lease_expires_at = ?
                 WHERE url = ? AND worker_id = ? AND status = 'in_progress'
                """,
                (new_expires, url, wid),
            )
            return res.rowcount > 0

    # ─── RESULT WRITES ──────────────────────────────────────────────────────

    def mark_completed(self, url: str, wid: str,
                       details: dict, html_path: Optional[str] = None) -> bool:
        """
        Persist extracted fields and flip status to 'completed'.
        Only succeeds if `wid` still owns the row — otherwise the result is
        discarded (a lease-expired re-claim has already happened).
        """
        payload = json.dumps(details, ensure_ascii=False, sort_keys=True)
        with self._tx() as cur:
            res = cur.execute(
                """
                UPDATE places
                   SET status        = 'completed',
                       details_json  = ?,
                       html_path     = COALESCE(?, html_path),
                       completed_at  = datetime('now'),
                       last_error    = NULL,
                       worker_id     = NULL,
                       lease_expires_at = NULL
                 WHERE url = ? AND worker_id = ?
                """,
                (payload, html_path, url, wid),
            )
            return res.rowcount > 0

    def mark_failed(self, url: str, wid: str, error: str) -> bool:
        """
        Record a failure. The row stays eligible for retry until attempts hits
        MAX_ATTEMPTS, at which point claim_next() will stop picking it up.
        """
        with self._tx() as cur:
            res = cur.execute(
                """
                UPDATE places
                   SET status     = 'failed',
                       last_error = ?,
                       worker_id  = NULL,
                       lease_expires_at = NULL
                 WHERE url = ? AND worker_id = ?
                """,
                (error[:2000], url, wid),
            )
            return res.rowcount > 0

    # ─── INTROSPECTION ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._tx() as cur:
            rows = cur.execute(
                "SELECT status, COUNT(*) AS n FROM places GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def reset_stale(self, reset_failed: bool = False) -> dict[str, int]:
        """
        Force-release any in_progress rows left by a crashed worker,
        and optionally reset failed rows so they're retried from scratch.
        Returns counts of rows affected per status.
        """
        affected: dict[str, int] = {}
        with self._tx() as cur:
            res = cur.execute(
                """
                UPDATE places
                   SET status = 'pending', worker_id = NULL,
                       lease_expires_at = NULL
                 WHERE status = 'in_progress'
                """
            )
            affected["in_progress"] = res.rowcount
            if reset_failed:
                res = cur.execute(
                    """
                    UPDATE places
                       SET status = 'pending', attempts = 0,
                           last_error = NULL, worker_id = NULL,
                           lease_expires_at = NULL
                     WHERE status = 'failed'
                    """
                )
                affected["failed"] = res.rowcount
        return affected

    # ─── INTERNAL ───────────────────────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """
        IMMEDIATE transaction → grabs the write lock up front, so a SELECT
        inside the tx can't be invalidated by another writer between SELECT
        and UPDATE. This is what makes claim_next() race-free.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            yield cur
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()
