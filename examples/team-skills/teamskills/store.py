"""SQLite persistence. One file holds the whole team catalog."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    token_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, handle)
);
CREATE INDEX IF NOT EXISTS members_token ON members(token_hash);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_handle TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    lifecycle TEXT NOT NULL DEFAULT 'active',
    deprecation_reason TEXT,
    draft_content TEXT,
    draft_editor TEXT,
    draft_updated_at TEXT,
    latest_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(team_id, slug)
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    author TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(skill_id, version)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    submitted_by TEXT NOT NULL,
    bump TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision TEXT,
    decided_by TEXT,
    reason TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS reviews_pending ON reviews(skill_id, decision);
CREATE TABLE IF NOT EXISTS installs (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    UNIQUE(team_id, handle, skill_id, target)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    skill_slug TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_team ON activity(team_id, id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class Store:
    """Thin wrapper around one SQLite connection, serialized with a lock.

    Every public method runs inside ``tx()``; callers that need several
    statements to be atomic wrap them in one ``with store.tx():`` block
    (the lock is re-entrant and the transaction commits when the outermost
    block exits).
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL") if self.path != ":memory:" else None
        self._lock = threading.RLock()
        self._depth = 0
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    # -- generic helpers -------------------------------------------------
    def one(self, sql: str, params=()) -> dict | None:
        with self.tx() as c:
            return _row(c.execute(sql, params).fetchone())

    def all(self, sql: str, params=()) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def run(self, sql: str, params=()) -> int:
        """Execute a statement and return ``lastrowid``."""
        with self.tx() as c:
            return c.execute(sql, params).lastrowid

    # -- teams -----------------------------------------------------------
    def insert_team(self, slug: str, name: str) -> int:
        return self.run("INSERT INTO teams (slug, name, created_at) VALUES (?, ?, ?)", (slug, name, now()))

    def team_by_slug(self, slug: str) -> dict | None:
        return self.one("SELECT * FROM teams WHERE slug = ?", (slug,))

    def teams(self) -> list[dict]:
        return self.all("SELECT * FROM teams ORDER BY slug")

    # -- members ---------------------------------------------------------
    def insert_member(self, team_id: int, handle: str, display_name: str, role: str, token_hash: str | None) -> int:
        return self.run(
            "INSERT INTO members (team_id, handle, display_name, role, token_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, handle, display_name, role, token_hash, now()),
        )

    def member(self, team_id: int, handle: str) -> dict | None:
        return self.one("SELECT * FROM members WHERE team_id = ? AND handle = ?", (team_id, handle))

    def member_by_token_hash(self, token_hash: str) -> dict | None:
        return self.one(
            "SELECT m.*, t.slug AS team_slug FROM members m JOIN teams t ON t.id = m.team_id WHERE m.token_hash = ?",
            (token_hash,),
        )

    def members(self, team_id: int) -> list[dict]:
        return self.all("SELECT * FROM members WHERE team_id = ? ORDER BY handle", (team_id,))

    def update_member(self, member_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE members SET {cols} WHERE id = ?", (*fields.values(), member_id))

    def delete_member(self, member_id: int) -> None:
        self.run("DELETE FROM members WHERE id = ?", (member_id,))

    # -- skills ----------------------------------------------------------
    def insert_skill(self, team_id: int, slug: str, title: str, description: str, owner: str, tags: list[str], content: str, editor: str) -> int:
        ts = now()
        return self.run(
            "INSERT INTO skills (team_id, slug, title, description, owner_handle, tags, draft_content, draft_editor, draft_updated_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, slug, title, description, owner, json.dumps(tags), content, editor, ts, ts, ts),
        )

    def skill(self, team_id: int, slug: str) -> dict | None:
        return self.one("SELECT * FROM skills WHERE team_id = ? AND slug = ?", (team_id, slug))

    def skill_by_id(self, skill_id: int) -> dict | None:
        return self.one("SELECT * FROM skills WHERE id = ?", (skill_id,))

    def skills(self, team_id: int) -> list[dict]:
        return self.all("SELECT * FROM skills WHERE team_id = ? ORDER BY slug", (team_id,))

    def update_skill(self, skill_id: int, **fields) -> None:
        fields["updated_at"] = now()
        if "tags" in fields and not isinstance(fields["tags"], str):
            fields["tags"] = json.dumps(fields["tags"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE skills SET {cols} WHERE id = ?", (*fields.values(), skill_id))

    def delete_skill(self, skill_id: int) -> None:
        self.run("DELETE FROM skills WHERE id = ?", (skill_id,))

    # -- versions --------------------------------------------------------
    def insert_version(self, skill_id: int, version: str, content: str, content_hash: str, author: str, approved_by: str, note: str) -> int:
        return self.run(
            "INSERT INTO versions (skill_id, version, content, content_hash, author, approved_by, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (skill_id, version, content, content_hash, author, approved_by, note, now()),
        )

    def version_by_id(self, version_id: int) -> dict | None:
        return self.one("SELECT * FROM versions WHERE id = ?", (version_id,))

    def version(self, skill_id: int, version: str) -> dict | None:
        return self.one("SELECT * FROM versions WHERE skill_id = ? AND version = ?", (skill_id, version))

    def versions(self, skill_id: int) -> list[dict]:
        return self.all("SELECT * FROM versions WHERE skill_id = ? ORDER BY id DESC", (skill_id,))

    # -- reviews ---------------------------------------------------------
    def insert_review(self, skill_id: int, submitted_by: str, bump: str, note: str, content_hash: str) -> int:
        return self.run(
            "INSERT INTO reviews (skill_id, submitted_by, bump, note, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (skill_id, submitted_by, bump, note, content_hash, now()),
        )

    def pending_review(self, skill_id: int) -> dict | None:
        return self.one("SELECT * FROM reviews WHERE skill_id = ? AND decision IS NULL ORDER BY id DESC LIMIT 1", (skill_id,))

    def pending_reviews(self, team_id: int) -> list[dict]:
        return self.all(
            "SELECT r.*, s.slug AS skill_slug, s.title AS skill_title FROM reviews r JOIN skills s ON s.id = r.skill_id"
            " WHERE s.team_id = ? AND r.decision IS NULL ORDER BY r.id",
            (team_id,),
        )

    def reviews(self, skill_id: int, limit: int = 20) -> list[dict]:
        return self.all("SELECT * FROM reviews WHERE skill_id = ? ORDER BY id DESC LIMIT ?", (skill_id, limit))

    def decide_review(self, review_id: int, decision: str, decided_by: str, reason: str) -> None:
        self.run(
            "UPDATE reviews SET decision = ?, decided_by = ?, reason = ?, decided_at = ? WHERE id = ?",
            (decision, decided_by, reason, now(), review_id),
        )

    # -- installs --------------------------------------------------------
    def upsert_install(self, team_id: int, handle: str, skill_id: int, version: str, target: str, path: str, content_hash: str) -> None:
        self.run(
            "INSERT INTO installs (team_id, handle, skill_id, version, target, path, content_hash, installed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(team_id, handle, skill_id, target) DO UPDATE SET"
            " version = excluded.version, path = excluded.path, content_hash = excluded.content_hash, installed_at = excluded.installed_at",
            (team_id, handle, skill_id, version, target, path, content_hash, now()),
        )

    def installs(self, team_id: int, handle: str | None = None) -> list[dict]:
        sql = (
            "SELECT i.*, s.slug AS skill_slug FROM installs i JOIN skills s ON s.id = i.skill_id WHERE i.team_id = ?"
        )
        params: tuple = (team_id,)
        if handle is not None:
            sql += " AND i.handle = ?"
            params += (handle,)
        return self.all(sql + " ORDER BY i.handle, s.slug, i.target", params)

    def delete_install(self, team_id: int, handle: str, skill_id: int, target: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM installs WHERE team_id = ? AND handle = ? AND skill_id = ? AND target = ?",
                (team_id, handle, skill_id, target),
            )
            return cur.rowcount

    def install_counts(self, team_id: int) -> dict[int, int]:
        rows = self.all("SELECT skill_id, COUNT(*) AS n FROM installs WHERE team_id = ? GROUP BY skill_id", (team_id,))
        return {r["skill_id"]: r["n"] for r in rows}

    # -- activity --------------------------------------------------------
    def log(self, team_id: int, actor: str, action: str, skill_slug: str | None, details: dict) -> None:
        self.run(
            "INSERT INTO activity (team_id, actor, action, skill_slug, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, actor, action, skill_slug, json.dumps(details), now()),
        )

    def activity(self, team_id: int, limit: int = 50, skill_slug: str | None = None) -> list[dict]:
        sql = "SELECT * FROM activity WHERE team_id = ?"
        params: tuple = (team_id,)
        if skill_slug:
            sql += " AND skill_slug = ?"
            params += (skill_slug,)
        rows = self.all(sql + " ORDER BY id DESC LIMIT ?", params + (limit,))
        for r in rows:
            r["details"] = json.loads(r["details"])
        return rows
