import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from .values import canonical, parse_json, fingerprint, reject, key, integer

SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meters(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,meter TEXT NOT NULL,start INTEGER NOT NULL,
 end INTEGER,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS segment_time ON segments(meter,start);
CREATE TABLE IF NOT EXISTS readings(id TEXT PRIMARY KEY,segment TEXT NOT NULL,at INTEGER NOT NULL,
 value INTEGER NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,
 UNIQUE(segment,at));
CREATE TABLE IF NOT EXISTS reading_history(id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
 digest TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,meter TEXT NOT NULL,start INTEGER NOT NULL,
 end INTEGER NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS schedule_time ON schedules(meter,start,end);
CREATE TABLE IF NOT EXISTS rates(id TEXT PRIMARY KEY,meter TEXT NOT NULL,start INTEGER NOT NULL,
 end INTEGER NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS factors(id TEXT PRIMARY KEY,meter TEXT NOT NULL,start INTEGER NOT NULL,
 end INTEGER NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closures(id TEXT PRIMARY KEY,start INTEGER NOT NULL,end INTEGER NOT NULL,
 state TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,closure TEXT NOT NULL,payload TEXT NOT NULL,
 digest TEXT NOT NULL,approved INTEGER NOT NULL,operator TEXT);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
 entity TEXT NOT NULL,payload TEXT NOT NULL,previous TEXT NOT NULL,digest TEXT NOT NULL);
"""


class Store:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:" and Path(path).is_symlink():
            reject("path", "database may not be a symlink")
        self.path = str(path)
        self.lock = threading.RLock()
        self.depth = 0
        self.connection = sqlite3.connect(
            path, isolation_level=None, timeout=10, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.executescript(SCHEMA)
        version = self.one("SELECT value FROM configuration WHERE name='schema'")
        if version and version["value"] != "1":
            self.connection.close()
            reject("schema", "unsupported storage version")
        self.connection.execute(
            "INSERT OR IGNORE INTO configuration VALUES('schema','1')"
        )

    @contextmanager
    def atomic(self):
        with self.lock:
            level = self.depth
            self.connection.execute(
                "BEGIN IMMEDIATE" if level == 0 else f"SAVEPOINT unit{level}"
            )
            self.depth += 1
            try:
                yield self.connection
            except BaseException:
                if level == 0:
                    self.connection.execute("ROLLBACK")
                else:
                    self.connection.execute(f"ROLLBACK TO unit{level}")
                    self.connection.execute(f"RELEASE unit{level}")
                raise
            else:
                self.connection.execute(
                    "COMMIT" if level == 0 else f"RELEASE unit{level}"
                )
            finally:
                self.depth -= 1

    def rows(self, statement, arguments=()):
        with self.lock:
            return [dict(row) for row in self.connection.execute(statement, arguments)]

    def one(self, statement, arguments=()):
        values = self.rows(statement, arguments)
        return values[0] if values else None

    def event(self, kind, entity, payload):
        if self.depth == 0:
            reject("state", "audit events require an active write transaction")
        previous = self.one("SELECT seq,digest FROM audit ORDER BY seq DESC LIMIT 1")
        sequence = previous["seq"] + 1 if previous else 1
        head = previous["digest"] if previous else "0" * 64
        data = {
            "seq": sequence,
            "kind": kind,
            "entity": entity,
            "payload": payload,
            "previous": head,
        }
        self.connection.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?)",
            (sequence, kind, entity, canonical(payload), head, fingerprint(data)),
        )
        return sequence

    def verify_audit(self):
        previous = "0" * 64
        rows = self.rows("SELECT * FROM audit ORDER BY seq")
        for sequence, row in enumerate(rows, 1):
            data = {
                "seq": sequence,
                "kind": row["kind"],
                "entity": row["entity"],
                "payload": parse_json(row["payload"]),
                "previous": previous,
            }
            if (
                row["seq"] != sequence
                or row["previous"] != previous
                or row["digest"] != fingerprint(data)
            ):
                reject("integrity", "audit chain differs", sequence=sequence)
            previous = row["digest"]
        return {"count": len(rows), "head": previous, "verified": True}

    def audit_page(self, after=0, limit=100):
        integer(after)
        integer(limit, 1, 1000)
        rows = self.rows(
            "SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit + 1)
        )
        result = rows[:limit]
        for row in result:
            row["payload"] = parse_json(row["payload"])
        return {
            "rows": result,
            "next": result[-1]["seq"] if len(rows) > limit else None,
        }

    def payload(self, table, ident):
        allowed = {
            "meters",
            "segments",
            "readings",
            "schedules",
            "rates",
            "factors",
            "closures",
            "corrections",
        }
        if table not in allowed:
            reject("input", "unknown payload collection")
        row = self.one("SELECT * FROM " + table + " WHERE id=?", (ident,))
        if row is None:
            reject("missing", "record not found", collection=table, id=ident)
        value = parse_json(row["payload"])
        if fingerprint(value) != row["digest"]:
            reject("integrity", "record payload changed", collection=table, id=ident)
        return row, value

    def integrity(self):
        check = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            reject("integrity", "SQLite integrity check failed", result=check)
        counts = {}
        for table in [
            "meters",
            "segments",
            "readings",
            "schedules",
            "rates",
            "factors",
            "closures",
            "corrections",
        ]:
            rows = self.rows("SELECT id FROM " + table + " ORDER BY id")
            for row in rows:
                self.payload(table, row["id"])
            counts[table] = len(rows)
        for row in self.rows("SELECT * FROM reading_history"):
            if fingerprint(parse_json(row["payload"])) != row["digest"]:
                reject(
                    "integrity",
                    "reading history changed",
                    id=row["id"],
                    revision=row["revision"],
                )
        return {"verified": True, "counts": counts, "audit": self.verify_audit()}

    def snapshot_signature(self):
        tables = {}
        for table in [
            "meters",
            "segments",
            "readings",
            "schedules",
            "rates",
            "factors",
        ]:
            tables[table] = self.rows("SELECT id,digest FROM " + table + " ORDER BY id")
        return fingerprint(tables)

    def backup(self, path):
        target = Path(path)
        if target.exists() or target.is_symlink():
            reject("path", "backup destination must be new")
        with self.lock:
            if self.depth:
                reject("state", "backup requires no active transaction")
            connection = sqlite3.connect(target)
            try:
                self.connection.backup(connection)
            finally:
                connection.close()
        return {"path": str(target), "bytes": target.stat().st_size}

    def close(self):
        if self.depth:
            reject("state", "cannot close active transaction")
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
