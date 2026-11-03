"""
db_v5.py — STEP 5: two NEW tables, added to the same agent.db file.
(db.py from Step 3 is unchanged; this file only adds.)

    owner_facts    things that are true about you RIGHT NOW, which the bot may use in replies.
                   Example: "Free on Friday evening", "In Chittagong until 10 Nov".
                   Each fact can have an end date, after which it is ignored automatically.

    decision_log   one row per message the brain handled: the draft, the grounding check,
                   every step the graph took, and the final decision. Used to debug and,
                   later, to measure how often the bot is right.
"""

import json
from datetime import date

import db

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS owner_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT NOT NULL,
    valid_until  TEXT,                      -- 'YYYY-MM-DD' or NULL = no end date
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id  TEXT,
    number         TEXT NOT NULL,
    incoming_text  TEXT NOT NULL DEFAULT '',
    action         TEXT NOT NULL,           -- reply / hold / ignore
    reply_text     TEXT,
    reason         TEXT NOT NULL DEFAULT '',
    draft_json     TEXT,                    -- what the drafting AI returned
    grounding_json TEXT,                    -- what the grounding check found
    trace_json     TEXT,                    -- the steps the graph took
    created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_number ON decision_log(number, created_at);
"""


def init_db_v5() -> None:
    db.init_db()  # Step 3 tables (no-op if they exist)
    with db.connect() as conn:
        conn.executescript(SCHEMA_V5)


# ---------- owner facts ----------

def add_fact(text: str, valid_until: str | None = None) -> dict:
    if valid_until:
        date.fromisoformat(valid_until)  # raises ValueError if not YYYY-MM-DD
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO owner_facts (text, valid_until, created_at) VALUES (?, ?, ?)",
            (text.strip(), valid_until or None, db.now()),
        )
        row = conn.execute("SELECT * FROM owner_facts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_facts(include_expired: bool = False, today: date | None = None) -> list[dict]:
    today_s = (today or date.today()).isoformat()
    with db.connect() as conn:
        if include_expired:
            rows = conn.execute("SELECT * FROM owner_facts ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM owner_facts WHERE valid_until IS NULL OR valid_until >= ? ORDER BY id",
                (today_s,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_fact(fact_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM owner_facts WHERE id = ?", (fact_id,))
    return cur.rowcount == 1


# ---------- decision log ----------

def log_decision(state: dict) -> int:
    """Save what the graph did with one message. `state` is the graph's final state."""
    def as_json(value):
        return json.dumps(value, ensure_ascii=False) if value is not None else None

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO decision_log (wa_message_id, number, incoming_text, action, reply_text,
                                         reason, draft_json, grounding_json, trace_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.get("message_id"), state.get("number", ""), state.get("text", ""),
                state.get("action", "hold"), state.get("reply_text"), state.get("reason", ""),
                as_json(state.get("draft")), as_json(state.get("grounding")),
                as_json(state.get("trace", [])), db.now(),
            ),
        )
        return cur.lastrowid


def recent_decisions(limit: int = 20, number: str | None = None) -> list[dict]:
    with db.connect() as conn:
        if number:
            rows = conn.execute(
                "SELECT * FROM decision_log WHERE number = ? ORDER BY id DESC LIMIT ?", (number, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("draft_json", "grounding_json", "trace_json"):
            item[key.removesuffix("_json")] = json.loads(item.pop(key)) if item[key] else None
        out.append(item)
    return out


def human_reply_count(number: str) -> int:
    """How many messages YOU (not the bot) have ever sent to this contact."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE number = ? AND direction = 'out'
                 AND source IN ('owner_phone', 'export', 'owner_edit')""",
            (number,),
        ).fetchone()
    return row["n"]
