"""SQLite storage.

One file, WAL, one connection per thread (sqlite3 connections are not
shareable). Autocommit is on -- use :meth:`Database.transaction` when a group
of statements must land together.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Union

from projectos import paths

log = logging.getLogger(__name__)

#: Ordered schema migrations. Append, never edit: ``PRAGMA user_version``
#: records how many have been applied.
MIGRATIONS = [
    [
        """CREATE TABLE IF NOT EXISTS users (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               username TEXT UNIQUE NOT NULL,
               password_hash TEXT NOT NULL,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS sessions (
               token TEXT PRIMARY KEY,
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               created_at TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               user_agent TEXT
           )""",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
        """CREATE TABLE IF NOT EXISTS devices (
               id TEXT PRIMARY KEY,
               kind TEXT,
               name TEXT,
               address TEXT,
               port INTEGER,
               properties TEXT,
               capabilities TEXT,
               first_seen TEXT,
               last_seen TEXT,
               pinned INTEGER DEFAULT 0,
               ignored INTEGER DEFAULT 0,
               custom_name TEXT
           )""",
        "CREATE INDEX IF NOT EXISTS idx_devices_kind ON devices(kind)",
        "CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen)",
        """CREATE TABLE IF NOT EXISTS kv (
               namespace TEXT NOT NULL,
               key TEXT NOT NULL,
               value TEXT,
               PRIMARY KEY (namespace, key)
           )""",
        """CREATE TABLE IF NOT EXISTS log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT,
               level TEXT,
               source TEXT,
               message TEXT,
               data TEXT
           )""",
        "CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_level ON log(level)",
        """CREATE TABLE IF NOT EXISTS suggestion_state (
               id TEXT PRIMARY KEY,
               dismissed_at TEXT,
               applied_at TEXT
           )""",
    ],
    # -- helpers: the machines that joined this ProjectOS to lend it something
    [
        """CREATE TABLE IF NOT EXISTS helpers (
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               name TEXT,
               platform TEXT,
               token_hash TEXT NOT NULL,
               capabilities TEXT,
               facts TEXT,
               paired_at TEXT NOT NULL,
               last_seen TEXT,
               enabled INTEGER DEFAULT 1
           )""",
        "CREATE INDEX IF NOT EXISTS idx_helpers_kind ON helpers(kind)",
        "CREATE INDEX IF NOT EXISTS idx_helpers_last_seen ON helpers(last_seen)",
        # Pairing codes are rows rather than memory so a restart mid-pairing does
        # not silently invalidate the code someone is already typing.
        """CREATE TABLE IF NOT EXISTS helper_codes (
               code TEXT PRIMARY KEY,
               kind TEXT,
               created_at TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               used_at TEXT,
               helper_id TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS helper_jobs (
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               payload TEXT,
               needs TEXT,
               state TEXT NOT NULL,
               helper_id TEXT,
               result TEXT,
               error TEXT,
               created_at TEXT NOT NULL,
               started_at TEXT,
               finished_at TEXT,
               attempts INTEGER DEFAULT 0
           )""",
        "CREATE INDEX IF NOT EXISTS idx_helper_jobs_state ON helper_jobs(state, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_helper_jobs_helper ON helper_jobs(helper_id)",
    ],
]  # type: List[List[str]]

SCHEMA_NAMESPACE = "_schema"
DEFAULT_LOG_CAP = 2000
_TRIM_EVERY = 50


def utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


class Database(object):
    """Thin, honest wrapper around sqlite3."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        timeout: float = 30.0,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._explicit_path = path
        self._timeout = float(timeout)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        self._connections = []  # type: List[sqlite3.Connection]
        self._lock = threading.RLock()
        # Statements in flight, guarded so close() can wait for them. Connections
        # are opened with check_same_thread=False and worker threads (a BirdTunes
        # import, a helper job) run statements on them; closing a connection out
        # from under a running sqlite3_step is a use-after-free in the C module,
        # and it segfaults the process rather than raising. See _busy()/close().
        self._busy_gate = threading.Condition()
        self._inflight = 0
        self._schemas = {}  # type: Dict[str, str]
        self._log_writes = 0
        self._closed = False
        # ":memory:" would give every thread its own empty database, which is a
        # trap in tests; a shared-cache URI keeps one database per instance.
        self._memory = str(path) == ":memory:"
        self._memory_uri = "file:projectos-mem-%x?mode=memory&cache=shared" % id(self)

    # -- connections -----------------------------------------------------
    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return Path(self._explicit_path)
        return paths.db_file()

    def connect(self) -> sqlite3.Connection:
        """The connection for the calling thread, opening it if needed."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self._closed:
            raise RuntimeError("database is closed")
        if self._memory:
            conn = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=self._timeout,
                check_same_thread=False,
                isolation_level=None,
            )
        else:
            target = self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(target),
                timeout=self._timeout,
                check_same_thread=False,
                isolation_level=None,
            )
        conn.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA busy_timeout=%d" % self._busy_timeout_ms,
            "PRAGMA synchronous=NORMAL",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.DatabaseError as exc:  # e.g. WAL on :memory:
                log.debug("pragma failed (%s): %s", pragma, exc)
        self._local.conn = conn
        with self._lock:
            self._connections.append(conn)
        return conn

    @contextmanager
    def _busy(self) -> Iterator[None]:
        """Mark a statement as in flight so close() waits instead of racing it."""
        with self._busy_gate:
            self._inflight += 1
        try:
            yield
        finally:
            with self._busy_gate:
                self._inflight -= 1
                if self._inflight <= 0:
                    self._busy_gate.notify_all()

    def close(self, drain_timeout: float = 5.0) -> None:
        """Close every connection this instance opened, from any thread.

        Waits for in-flight statements first. A worker thread that is still
        inside sqlite3 when its connection is closed takes the whole process
        down with a segfault, so this waits rather than assuming shutdown order.
        If the wait times out we close anyway: a hung statement at shutdown is
        the lesser problem, and the timeout is logged.
        """
        with self._lock:
            self._closed = True
        deadline_reached = False
        with self._busy_gate:
            if self._inflight > 0:
                deadline_reached = not self._busy_gate.wait_for(
                    lambda: self._inflight <= 0, timeout=drain_timeout
                )
        if deadline_reached:
            log.warning(
                "closing the database with %d statement(s) still running", self._inflight
            )
        with self._lock:
            connections = list(self._connections)
            self._connections = []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - shutdown noise
                log.debug("closing connection failed: %s", exc)
        self._local = threading.local()

    # -- statements ------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self.connect()
        with self._busy():
            return conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        conn = self.connect()
        rows = [tuple(item) for item in seq]
        with self.transaction(), self._busy():
            conn.executemany(sql, rows)

    def executescript(self, script: str) -> None:
        conn = self.connect()
        with self._busy():
            conn.executescript(script)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        # The fetch steps sqlite too, so it belongs inside the same guard as the
        # execute -- not after it, where close() could land in between.
        conn = self.connect()
        with self._busy():
            return conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        conn = self.connect()
        with self._busy():
            return conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None else row[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN / COMMIT, rolling back on any exception."""
        conn = self.connect()
        if conn.in_transaction:
            # Nested use: the outermost block owns the transaction.
            yield conn
            return
        with self._busy():
            conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                with self._busy():
                    conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise
        else:
            with self._busy():
                conn.execute("COMMIT")

    # -- schema ----------------------------------------------------------
    def migrate(self) -> None:
        """Apply pending migrations. Safe to call on every boot."""
        with self._lock:
            conn = self.connect()
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row is not None else 0
            target = len(MIGRATIONS)
            if version >= target:
                return
            log.info("migrating database schema %d -> %d", version, target)
            with self.transaction():
                for index in range(version, target):
                    for statement in MIGRATIONS[index]:
                        conn.execute(statement)
                conn.execute("PRAGMA user_version = %d" % target)

    def register_schema(self, name: str, statements: List[str]) -> None:
        """Let an app create its own ``app_<id>_*`` tables.

        Idempotent twice over: the statements are expected to be
        ``CREATE ... IF NOT EXISTS``, and a fingerprint of them is stored so
        re-registering an unchanged schema is a no-op (which is what makes a
        later ``ALTER TABLE`` in the list safe to add).
        """
        statements = [s for s in (statements or []) if s and s.strip()]
        if not statements:
            return
        digest = hashlib.sha256("\n".join(statements).encode("utf-8")).hexdigest()
        with self._lock:
            if self._schemas.get(name) == digest:
                return
            if self.kv_get(SCHEMA_NAMESPACE, name) == digest:
                self._schemas[name] = digest
                return
            conn = self.connect()
            with self.transaction():
                for statement in statements:
                    conn.execute(statement)
            self.kv_set(SCHEMA_NAMESPACE, name, digest)
            self._schemas[name] = digest
            log.info("registered schema for %s (%d statements)", name, len(statements))

    def registered_schemas(self) -> List[str]:
        with self._lock:
            return sorted(self._schemas.keys())

    # -- key/value -------------------------------------------------------
    def kv_get(self, namespace: str, key: str, default: Any = None) -> Any:
        row = self.one(
            "SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key)
        )
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    def kv_set(self, namespace: str, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        self.execute(
            "INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (namespace, key, payload),
        )

    def kv_delete(self, namespace: str, key: str) -> None:
        self.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))

    def kv_all(self, namespace: str) -> Dict[str, Any]:
        out = {}  # type: Dict[str, Any]
        for row in self.query("SELECT key, value FROM kv WHERE namespace = ?", (namespace,)):
            try:
                out[row["key"]] = json.loads(row["value"]) if row["value"] is not None else None
            except (ValueError, TypeError):
                out[row["key"]] = row["value"]
        return out

    # -- log -------------------------------------------------------------
    def add_log(
        self,
        level: str,
        source: str,
        message: str,
        data: Any = None,
        cap: int = DEFAULT_LOG_CAP,
    ) -> None:
        payload = None
        if data is not None:
            try:
                payload = json.dumps(data, default=str)
            except (TypeError, ValueError):  # pragma: no cover
                payload = str(data)
        self.execute(
            "INSERT INTO log (ts, level, source, message, data) VALUES (?, ?, ?, ?, ?)",
            (utcnow_iso(), level, source, message, payload),
        )
        self._log_writes += 1
        if cap:
            # Trimming on every insert would be wasteful; the table is allowed
            # to overshoot slightly between sweeps.
            every = min(_TRIM_EVERY, max(1, int(cap) // 4))
            if self._log_writes % every == 0:
                self.trim_log(cap)

    def trim_log(self, cap: int = DEFAULT_LOG_CAP) -> None:
        if cap <= 0:
            return
        self.execute(
            "DELETE FROM log WHERE id < ("
            "  SELECT MIN(id) FROM (SELECT id FROM log ORDER BY id DESC LIMIT ?)"
            ")",
            (int(cap),),
        )

    def recent_log(
        self, limit: int = 200, level: Optional[str] = None, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM log"
        clauses = []  # type: List[str]
        params = []  # type: List[Any]
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        if source:
            clauses.append("source LIKE ?")
            params.append("%s%%" % source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return rows_to_dicts(self.query(sql, params))

    def __repr__(self) -> str:
        return "Database(path=%r)" % (":memory:" if self._memory else str(self.path))


__all__ = [
    "Database",
    "MIGRATIONS",
    "DEFAULT_LOG_CAP",
    "row_to_dict",
    "rows_to_dicts",
    "utcnow_iso",
]
