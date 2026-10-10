"""
main_v3.py — STEP 3 brain: now with a memory (SQLite) and an approval queue.

What's new compared with main.py:
  * every message in and out is saved (messages table)
  * every person who writes is remembered (contacts table)
  * by default NOTHING is sent automatically: the draft goes to the approval queue,
    and you approve, edit or reject it from http://127.0.0.1:8000/docs
  * approved replies wait in the "outbox"; the Step 3 bridge picks them up and sends them

Run:  uvicorn main_v3:app --reload --port 8000
"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # create the tables the first time
    print(f"Database ready at {db.DB_PATH}")
    yield


app = FastAPI(title="WhatsApp Agent Brain (Step 3)", lifespan=lifespan)


# ---------- data shapes ----------

class IncomingMessage(BaseModel):
    """Sent by the bridge when someone writes to you. Same shape as Step 2."""
    message_id: str
    chat_id: str
    sender_name: str
    sender_number: str
    type: str = "chat"
    text: str = ""
    has_media: bool = False
    timestamp: int


class OutgoingMessage(BaseModel):
    """Sent by the bridge when a message LEAVES your account (typed by you, or sent by the bot)."""
    message_id: str
    chat_id: str
    number: str
    text: str = ""
    type: str = "chat"
    timestamp: int
    source: Literal["owner_phone", "bot"]


class BrainDecision(BaseModel):
    action: Literal["reply", "hold", "ignore"]
    text: Optional[str] = None
    reason: str = ""


class ContactUpdate(BaseModel):
    display_name: Optional[str] = None
    relationship: Optional[str] = None
    language: Optional[str] = None
    auto_reply: Optional[int] = None  # 0 or 1
    notes: Optional[str] = None


class ApproveBody(BaseModel):
    text: Optional[str] = None  # leave empty to send the draft as it is


# ---------- the decision (simple rules for now; LangGraph + OpenAI later) ----------

def make_draft(msg: IncomingMessage) -> str:
    """Placeholder draft. In Step 4 this becomes an OpenAI reply in your style."""
    return f'Brain received: "{msg.text[:50]}"'


def decide(msg: IncomingMessage, contact: dict) -> tuple[BrainDecision, Optional[str]]:
    """Returns (decision for the bridge, draft to put in the approval queue or None)."""
    if msg.type != "chat" or not msg.text.strip():
        return BrainDecision(action="hold", reason=f"'{msg.type}' messages not supported yet"), None

    draft = make_draft(msg)
    if contact["auto_reply"] == 1:
        return BrainDecision(action="reply", text=draft, reason="auto_reply is on for this contact"), None

    return BrainDecision(action="hold", reason="auto_reply is off: waiting for Stanley"), draft


# ---------- endpoints used by the bridge ----------

@app.get("/health")
def health():
    return {"status": "ok", "pending": len(db.list_pending("pending"))}


@app.post("/incoming", response_model=BrainDecision)
def incoming(msg: IncomingMessage) -> BrainDecision:
    when = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
    print(f"[{when}] IN  {msg.sender_name} ({msg.sender_number}) [{msg.type}]: {msg.text}")

    contact = db.upsert_contact(msg.sender_number, msg.chat_id, msg.sender_name)
    is_new = db.save_message(msg.message_id, msg.chat_id, msg.sender_number, "in", "contact",
                             msg.type, msg.text, msg.timestamp)
    if not is_new:
        return BrainDecision(action="ignore", reason="duplicate message, already handled")

    decision, draft = decide(msg, contact)

    if decision.action == "hold":
        pid = db.add_pending(msg.chat_id, msg.sender_number, msg.message_id, draft, decision.reason)
        print(f"           -> HELD as pending #{pid}: {decision.reason}")
    return decision


@app.post("/outgoing")
def outgoing(msg: OutgoingMessage):
    """Remember what left your account. Your own typed replies are gold for learning your style."""
    db.save_message(msg.message_id, msg.chat_id, msg.number, "out", msg.source,
                    msg.type, msg.text, msg.timestamp)
    return {"saved": True}


@app.get("/outbox")
def outbox():
    """Approved replies the bridge should send now."""
    return db.list_pending("approved")


@app.post("/outbox/{pending_id}/sent")
def outbox_sent(pending_id: int):
    item = db.mark_sent(pending_id)
    if not item:
        raise HTTPException(404, "No such item")
    return item


# ---------- endpoints for YOU (use them from /docs; the dashboard comes in Step 8) ----------

@app.get("/pending")
def pending(status: Literal["pending", "approved", "rejected", "sent"] = "pending"):
    return db.list_pending(status)


@app.post("/pending/{pending_id}/approve")
def approve(pending_id: int, body: ApproveBody):
    item = db.decide_pending(pending_id, approve=True, final_text=body.text)
    if not item:
        raise HTTPException(400, "Not pending, not found, or no text to send (type a text to approve).")
    return item


@app.post("/pending/{pending_id}/reject")
def reject(pending_id: int):
    item = db.decide_pending(pending_id, approve=False)
    if not item:
        raise HTTPException(400, "Not pending or not found.")
    return item


@app.get("/contacts")
def contacts():
    return db.list_contacts()


@app.patch("/contacts/{number}")
def edit_contact(number: str, changes: ContactUpdate):
    if changes.auto_reply not in (None, 0, 1):
        raise HTTPException(400, "auto_reply must be 0 or 1")
    item = db.update_contact(number, changes.model_dump())
    if not item:
        raise HTTPException(404, "Contact not found (they must message you first).")
    return item


@app.get("/messages/{number}")
def messages(number: str, limit: int = 20):
    return db.recent_messages(number, limit)
