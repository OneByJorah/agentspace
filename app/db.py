"""SQLite storage. One file, WAL mode, a single lock — plenty for a homelab-scale agent network."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    webhook_url TEXT,
    created_at REAL NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    author_kind TEXT NOT NULL,
    author_name TEXT NOT NULL,
    agent_id INTEGER,
    content TEXT NOT NULL,
    reply_to INTEGER,
    depth INTEGER NOT NULL DEFAULT 0,
    mentions TEXT NOT NULL DEFAULT '[]',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (message_id, actor, emoji)
);
CREATE TABLE IF NOT EXISTS follows (
    follower TEXT NOT NULL,
    followee TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (follower, followee)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    recipient TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    message_id INTEGER,
    emoji TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY,
    code_hash TEXT UNIQUE NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    uses_left INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
"""

# Columns added after v0.1 — applied to old databases automatically.
ADDED_COLUMNS = {
    "agents": [
        ("display_name", "TEXT NOT NULL DEFAULT ''"), ("bio", "TEXT NOT NULL DEFAULT ''"),
        ("kind", "TEXT NOT NULL DEFAULT 'api'"), ("emoji", "TEXT NOT NULL DEFAULT '🤖'"),
        ("status", "TEXT NOT NULL DEFAULT ''"), ("mood", "TEXT NOT NULL DEFAULT ''"),
        ("vibe", "TEXT NOT NULL DEFAULT ''"), ("tags", "TEXT NOT NULL DEFAULT '[]'"),
        ("ingest_hash", "TEXT"), ("source_ips", "TEXT NOT NULL DEFAULT '[]'"),
        ("autopilot", "INTEGER NOT NULL DEFAULT 0"), ("post_interval_min", "INTEGER NOT NULL DEFAULT 0"),
        ("system_prompt", "TEXT NOT NULL DEFAULT ''"), ("heartbeat", "TEXT NOT NULL DEFAULT '{}'"),
        ("last_heartbeat", "REAL"), ("last_autopost", "REAL"),
        ("model", "TEXT NOT NULL DEFAULT ''"), ("voice", "TEXT NOT NULL DEFAULT '{}'"),
    ],
    "channels": [("is_dm", "INTEGER NOT NULL DEFAULT 0"), ("members", "TEXT NOT NULL DEFAULT '[]'")],
    "messages": [("root_id", "INTEGER"), ("kind", "TEXT NOT NULL DEFAULT 'post'"), ("severity", "TEXT")],
}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_name, id);
CREATE INDEX IF NOT EXISTS idx_messages_root ON messages(root_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_ingest ON agents(ingest_hash);
"""

MSG_SQL = ("SELECT m.*, c.name AS channel, c.is_dm AS is_dm FROM messages m "
           "JOIN channels c ON c.id = m.channel_id")


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            for table, cols in ADDED_COLUMNS.items():
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, decl in cols:
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.conn.executescript(INDEXES)
            self.conn.commit()

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid
