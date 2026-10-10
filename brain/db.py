"""
db.py — STEP 3: the agent's "notebook" (SQLite).

Three tables:
    contacts         who writes to you: name, relationship, language, notes,
                     and whether the bot may reply to them on its own (auto_reply)
    messages         every message in and out (later used to learn your style)
    pending_replies  drafts waiting for YOUR decision: pending -> approved/rejected -> sent

SQLite stores everything in one file: brain/data/agent.db
You can open it with the free "DB Browser for SQLite" app to look inside.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    number        TEXT PRIMARY KEY,
    chat_id       TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT 'Unknown',
    relationship  TEXT NOT NULL DEFAULT 'unknown',   -- friend, family, boss, colleague...
    language      TEXT NOT NULL DEFAULT 'unknown',   -- bangla, english, banglish
    auto_reply    INTEGER NOT NULL DEFAULT 0,        -- 0 = always ask Stanley, 1 = bot may reply
    notes         TEXT NOT NULL DEFAULT '',
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id  TEXT UNIQUE,                      -- WhatsApp's own id (prevents duplicates)
    chat_id        TEXT NOT NULL,
    number         TEXT NOT NULL,
    direction      TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    source         TEXT NOT NULL,                    -- contact / owner_phone / bot
    type           TEXT NOT NULL DEFAULT 'chat',
    text           TEXT NOT NULL DEFAULT '',
    timestamp      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_replies (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        TEXT NOT NULL,
    number         TEXT NOT NULL,
    in_reply_to    TEXT,                             -- wa_message_id of the incoming message
    draft_text     TEXT,                             -- what the bot suggests (may be empty)
    final_text     TEXT,                             -- what you approved (possibly edited)
    reason         TEXT NOT NULL DEFAULT '',         -- why it was held
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected', 'sent')),
    created_at     INTEGER NOT NULL,
    decided_at     INTEGER,
    sent_at        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_number ON messages(number, timestamp);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_replies(status);
"""

EDITABLE_CONTACT_FIELDS = {"display_name", "relationship", "language", "auto_reply", "notes"}


def now() -> int:
    return int(time.time())


@contextmanager
def connect():
    """Open the database, give it to the caller, save changes, close."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["text"]
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- contacts ----------

def upsert_contact(number: str, chat_id: str, display_name: str) -> dict:
    """Create the contact the first time we see them; otherwise update last_seen.
    The name you set yourself (via PATCH /contacts) is never overwritten."""
    t = now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO contacts (number, chat_id, display_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(number) DO UPDATE SET chat_id = excluded.chat_id,
                                                 last_seen = excluded.last_seen""",
            (number, chat_id, display_name or "Unknown", t, t),
        )
        row = conn.execute("SELECT * FROM contacts WHERE number = ?", (number,)).fetchone()
    return dict(row)


def get_contact(number: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE number = ?", (number,)).fetchone()
    return dict(row) if row else None


def list_contacts() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM contacts ORDER BY last_seen DESC").fetchall()
    return [dict(r) for r in rows]


def update_contact(number: str, changes: dict) -> dict | None:
    changes = {k: v for k, v in changes.items() if k in EDITABLE_CONTACT_FIELDS and v is not None}
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)
        with connect() as conn:
            conn.execute(f"UPDATE contacts SET {cols} WHERE number = ?", (*changes.values(), number))
    return get_contact(number)


# ---------- messages ----------

def save_message(wa_message_id, chat_id, number, direction, source, type_, text, timestamp) -> bool:
    """INSERT OR IGNORE: if the same WhatsApp message arrives twice, it is stored once.
    Returns True if it was new, False if we had already seen it."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages
               (wa_message_id, chat_id, number, direction, source, type, text, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wa_message_id, chat_id, number, direction, source, type_, text or "", timestamp),
        )
        return cur.rowcount == 1


def recent_messages(number: str, limit: int = 20) -> list[dict]:
    """Last N messages with this person, oldest first (how a chat reads)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE number = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (number, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- pending replies (the approval queue) ----------

def add_pending(chat_id, number, in_reply_to, draft_text, reason) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO pending_replies (chat_id, number, in_reply_to, draft_text, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, number, in_reply_to, draft_text, reason, now()),
        )
        return cur.lastrowid


def list_pending(status: str = "pending") -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, c.display_name FROM pending_replies p
               LEFT JOIN contacts c ON c.number = p.number
               WHERE p.status = ? ORDER BY p.created_at""",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending(pending_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pending_replies WHERE id = ?", (pending_id,)).fetchone()
    return dict(row) if row else None


def decide_pending(pending_id: int, approve: bool, final_text: str | None = None) -> dict | None:
    """Approve (optionally with your edited text) or reject. Only works on 'pending' items."""
    item = get_pending(pending_id)
    if not item or item["status"] != "pending":
        return None
    if approve:
        text = (final_text or item["draft_text"] or "").strip()
        if not text:
            return None  # nothing to send
        with connect() as conn:
            conn.execute(
                "UPDATE pending_replies SET status='approved', final_text=?, decided_at=? WHERE id=?",
                (text, now(), pending_id),
            )
    else:
        with connect() as conn:
            conn.execute(
                "UPDATE pending_replies SET status='rejected', decided_at=? WHERE id=?",
                (now(), pending_id),
            )
    return get_pending(pending_id)


def mark_sent(pending_id: int) -> dict | None:
    with connect() as conn:
        conn.execute(
            "UPDATE pending_replies SET status='sent', sent_at=? WHERE id=? AND status='approved'",
            (now(), pending_id),
        )
    return get_pending(pending_id)
