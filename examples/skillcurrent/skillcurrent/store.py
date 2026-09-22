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
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    UNIQUE(skill_id, channel)
);
CREATE TABLE IF NOT EXISTS channel_history (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    previous_version TEXT,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    pattern TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'team',
    rationale TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, name)
);
CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    passed INTEGER NOT NULL,
    results TEXT NOT NULL,
    run_by TEXT NOT NULL,
    run_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS check_runs_skill ON check_runs(skill_id, id);
CREATE TABLE IF NOT EXISTS installs (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'production',
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    UNIQUE(team_id, handle, host, skill_id, target)
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    event TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_env ON receipts(team_id, skill_id, handle, host, target, id);
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
CREATE TABLE IF NOT EXISTS beta_signups (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    team_size TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
        self._migrate()
        self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Bring a database created by an earlier layout up to date.

        Only the ``installs`` table has changed shape (it gained ``host`` and
        ``channel``); install records from the old layout are dropped and
        members simply run ``install`` again.
        """
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(installs)").fetchall()]
        if cols and "host" not in cols:
            self.conn.execute("DROP TABLE installs")

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

    # -- channels --------------------------------------------------------
    def channel(self, skill_id: int, channel: str) -> dict | None:
        return self.one(
            "SELECT c.*, v.version, v.content_hash, v.created_at AS published_at FROM channels c JOIN versions v ON v.id = c.version_id"
            " WHERE c.skill_id = ? AND c.channel = ?",
            (skill_id, channel),
        )

    def channels(self, skill_id: int) -> dict[str, dict]:
        rows = self.all(
            "SELECT c.*, v.version, v.content_hash, v.created_at AS published_at FROM channels c JOIN versions v ON v.id = c.version_id"
            " WHERE c.skill_id = ? ORDER BY c.channel",
            (skill_id,),
        )
        return {r["channel"]: r for r in rows}

    def set_channel(self, skill_id: int, channel: str, version_id: int, version: str, previous: str | None, kind: str, reason: str, set_by: str) -> None:
        with self.tx():
            self.run(
                "INSERT INTO channels (skill_id, channel, version_id, set_by, set_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(skill_id, channel) DO UPDATE SET version_id = excluded.version_id, set_by = excluded.set_by, set_at = excluded.set_at",
                (skill_id, channel, version_id, set_by, now()),
            )
            self.run(
                "INSERT INTO channel_history (skill_id, channel, version, previous_version, kind, reason, set_by, set_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (skill_id, channel, version, previous, kind, reason, set_by, now()),
            )

    def channel_history(self, skill_id: int, channel: str | None = None) -> list[dict]:
        sql = "SELECT * FROM channel_history WHERE skill_id = ?"
        params: tuple = (skill_id,)
        if channel:
            sql += " AND channel = ?"
            params += (channel,)
        return self.all(sql + " ORDER BY id DESC", params)

    # -- rules and check runs --------------------------------------------
    def insert_rule(self, team_id: int, name: str, kind: str, pattern: str, category: str, rationale: str, created_by: str) -> int:
        return self.run(
            "INSERT INTO rules (team_id, name, kind, pattern, category, rationale, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, name, kind, pattern, category, rationale, created_by, now()),
        )

    def rules(self, team_id: int) -> list[dict]:
        return self.all("SELECT * FROM rules WHERE team_id = ? ORDER BY id", (team_id,))

    def rule(self, team_id: int, name: str) -> dict | None:
        return self.one("SELECT * FROM rules WHERE team_id = ? AND name = ?", (team_id, name))

    def delete_rule(self, rule_id: int) -> None:
        self.run("DELETE FROM rules WHERE id = ?", (rule_id,))

    def insert_check_run(self, skill_id: int, content_hash: str, passed: bool, results: list, run_by: str) -> int:
        return self.run(
            "INSERT INTO check_runs (skill_id, content_hash, passed, results, run_by, run_at) VALUES (?, ?, ?, ?, ?, ?)",
            (skill_id, content_hash, int(passed), json.dumps(results), run_by, now()),
        )

    def latest_check_run(self, skill_id: int) -> dict | None:
        row = self.one("SELECT * FROM check_runs WHERE skill_id = ? ORDER BY id DESC LIMIT 1", (skill_id,))
        if row:
            row["results"] = json.loads(row["results"])
            row["passed"] = bool(row["passed"])
        return row

    # -- installs --------------------------------------------------------
    def upsert_install(self, team_id: int, handle: str, host: str, skill_id: int, version: str, channel: str, target: str, path: str, content_hash: str) -> None:
        self.run(
            "INSERT INTO installs (team_id, handle, host, skill_id, version, channel, target, path, content_hash, installed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(team_id, handle, host, skill_id, target) DO UPDATE SET"
            " version = excluded.version, channel = excluded.channel, path = excluded.path, content_hash = excluded.content_hash, installed_at = excluded.installed_at",
            (team_id, handle, host, skill_id, version, channel, target, path, content_hash, now()),
        )

    def installs(self, team_id: int, handle: str | None = None, host: str | None = None, skill_id: int | None = None) -> list[dict]:
        sql = "SELECT i.*, s.slug AS skill_slug FROM installs i JOIN skills s ON s.id = i.skill_id WHERE i.team_id = ?"
        params: tuple = (team_id,)
        if handle is not None:
            sql += " AND i.handle = ?"
            params += (handle,)
        if host is not None:
            sql += " AND i.host = ?"
            params += (host,)
        if skill_id is not None:
            sql += " AND i.skill_id = ?"
            params += (skill_id,)
        return self.all(sql + " ORDER BY i.handle, i.host, s.slug, i.target", params)

    def delete_install(self, team_id: int, handle: str, host: str, skill_id: int, target: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM installs WHERE team_id = ? AND handle = ? AND host = ? AND skill_id = ? AND target = ?",
                (team_id, handle, host, skill_id, target),
            )
            return cur.rowcount

    def install_counts(self, team_id: int) -> dict[int, int]:
        rows = self.all("SELECT skill_id, COUNT(*) AS n FROM installs WHERE team_id = ? GROUP BY skill_id", (team_id,))
        return {r["skill_id"]: r["n"] for r in rows}

    # -- receipts --------------------------------------------------------
    def insert_receipt(self, team_id: int, handle: str, host: str, target: str, skill_id: int, version: str, event: str, content_hash: str, detail: str) -> int:
        return self.run(
            "INSERT INTO receipts (team_id, handle, host, target, skill_id, version, event, content_hash, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, handle, host, target, skill_id, version, event, content_hash, detail, now()),
        )

    def receipts(self, team_id: int, skill_id: int | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT r.*, s.slug AS skill_slug FROM receipts r JOIN skills s ON s.id = r.skill_id WHERE r.team_id = ?"
        params: tuple = (team_id,)
        if skill_id is not None:
            sql += " AND r.skill_id = ?"
            params += (skill_id,)
        return self.all(sql + " ORDER BY r.id DESC LIMIT ?", params + (limit,))

    # -- beta sign-ups (landing page waitlist) ----------------------------
    def upsert_beta_signup(self, email: str, team_size: str, tools: list[str], note: str, source: str) -> None:
        ts = now()
        self.run(
            "INSERT INTO beta_signups (email, team_size, tools, note, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET team_size = excluded.team_size, tools = excluded.tools, note = excluded.note,"
            " source = excluded.source, updated_at = excluded.updated_at",
            (email, team_size, json.dumps(tools), note, source, ts, ts),
        )

    def beta_signups(self) -> list[dict]:
        rows = self.all("SELECT * FROM beta_signups ORDER BY id DESC")
        for r in rows:
            r["tools"] = json.loads(r["tools"])
        return rows

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
