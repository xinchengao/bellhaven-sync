"""SQLite store for proposals and decisions.

Each proposal has a deterministic key built from what it would change. A rerun
that produces the same key for an already approved/rejected proposal skips it,
so decided items are never re-proposed.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("BH_DB", "bellhaven.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    account_id  TEXT,
    location    TEXT,
    actions     TEXT NOT NULL,   -- JSON list of API operations
    evidence    TEXT NOT NULL,   -- JSON dict shown to the reviewer
    confidence  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/failed
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    decided_at  TEXT,
    result      TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    locations   INTEGER,
    accounts    INTEGER,
    new_pending INTEGER,
    skipped_decided INTEGER,
    summary     TEXT
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def proposal_key(kind, account_id, location, actions):
    payload = json.dumps([kind, account_id, location, actions], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def upsert_proposal(con, p):
    """Insert a new pending proposal. Returns 'new', 'pending' or 'decided'."""
    key = proposal_key(p["kind"], p.get("account_id"), p.get("location"), p["actions"])
    row = con.execute("SELECT status FROM proposals WHERE key=?", (key,)).fetchone()
    ts = now()
    if row is None:
        con.execute(
            "INSERT INTO proposals (key, kind, title, account_id, location, actions,"
            " evidence, confidence, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, p["kind"], p["title"], p.get("account_id"), p.get("location"),
             json.dumps(p["actions"]), json.dumps(p["evidence"]), p["confidence"], ts, ts),
        )
        return "new"
    if row["status"] in ("pending", "obsolete"):  # obsolete = vanished earlier, now back
        con.execute("UPDATE proposals SET status='pending', decided_at=NULL, last_seen=?,"
                    " title=?, evidence=? WHERE key=?",
                    (ts, p["title"], json.dumps(p["evidence"]), key))
        return "pending"
    return "decided"


def expire_stale(con, seen_keys):
    """Pending proposals the latest run no longer produces are obsolete (CRM changed)."""
    rows = con.execute("SELECT key FROM proposals WHERE status='pending'").fetchall()
    stale = [r["key"] for r in rows if r["key"] not in seen_keys]
    for k in stale:
        con.execute("UPDATE proposals SET status='obsolete', decided_at=? WHERE key=?", (now(), k))
    return len(stale)


def record_decision(con, key, status, result=None):
    con.execute("UPDATE proposals SET status=?, decided_at=?, result=? WHERE key=?",
                (status, now(), json.dumps(result) if result is not None else None, key))
    con.commit()
