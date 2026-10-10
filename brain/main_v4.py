"""
main_v4.py — STEP 4 brain: real drafts in YOUR style (OpenAI + your past messages).

Same endpoints as main_v3.py, plus:
  * make_draft() now asks OpenAI, using style examples (style.py)
  * every draft comes with a confidence score and a needs_owner flag
  * POST /test-draft lets you try drafts in the browser WITHOUT WhatsApp

When can something be sent without you?  ALL of these must be true:
  - the contact has auto_reply = 1
  - the AI did not set needs_owner
  - confidence >= AUTO_SEND_MIN_CONFIDENCE (from .env, default 0.85)
Otherwise it is held in the approval queue, with the AI's draft ready for you.

Run:  uvicorn main_v4:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import db
import llm
import style

AUTO_SEND_MIN_CONFIDENCE = float(os.getenv("AUTO_SEND_MIN_CONFIDENCE", "0.85"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    print(f"Database ready at {db.DB_PATH}")
    print(f"Model: {llm.MODEL} | owner: {style.OWNER_NAME} | auto-send needs confidence >= {AUTO_SEND_MIN_CONFIDENCE}")
    yield


app = FastAPI(title="WhatsApp Agent Brain (Step 4)", lifespan=lifespan)


# ---------- data shapes (same as Step 3, plus TestDraft) ----------

class IncomingMessage(BaseModel):
    message_id: str
    chat_id: str
    sender_name: str
    sender_number: str
    type: str = "chat"
    text: str = ""
    has_media: bool = False
    timestamp: int


class OutgoingMessage(BaseModel):
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
    auto_reply: Optional[int] = None
    notes: Optional[str] = None


class ApproveBody(BaseModel):
    text: Optional[str] = None


class TestDraft(BaseModel):
    number: str               # a contact that already exists (see GET /contacts)
    text: str                 # pretend they just sent this
    show_prompt: bool = False # True = also show exactly what was sent to OpenAI


# ---------- the decision ----------

def make_draft(contact: dict, text: str, message_id: str | None = None) -> dict:
    system, user, n_examples = style.build_prompts(contact, text, message_id)
    draft = llm.draft_json(system, user)
    draft["examples_used"] = n_examples
    draft["_prompt"] = {"system": system, "user": user}
    return draft


def decide(msg: IncomingMessage, contact: dict) -> tuple[BrainDecision, Optional[str]]:
    if msg.type != "chat" or not msg.text.strip():
        return BrainDecision(action="hold", reason=f"'{msg.type}' messages not supported yet"), None

    try:
        d = make_draft(contact, msg.text, msg.message_id)
    except Exception as err:  # no key, no internet, timeout... -> never guess, hold it
        return BrainDecision(action="hold", reason=f"AI error: {err}"[:300]), None

    summary = f"confidence {d['confidence']:.2f}, {d['examples_used']} examples. {d['reason']}"
    print(f"           draft: {d['reply']!r} ({summary}; needs_owner={d['needs_owner']})")

    can_auto = (contact["auto_reply"] == 1
                and not d["needs_owner"]
                and d["confidence"] >= AUTO_SEND_MIN_CONFIDENCE)
    if can_auto:
        return BrainDecision(action="reply", text=d["reply"], reason=summary), None

    why = "needs you" if d["needs_owner"] else ("auto_reply off" if contact["auto_reply"] != 1 else "not confident")
    return BrainDecision(action="hold", reason=f"{why}: {summary}"), d["reply"]


# ---------- endpoints used by the bridge (unchanged from Step 3) ----------

@app.get("/health")
def health():
    return {"status": "ok", "model": llm.MODEL, "pending": len(db.list_pending("pending"))}


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


def was_written_by_owner(number: str, text: str) -> bool:
    """True if this bot-sent text is one YOU typed while approving (edited draft or no draft)."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM pending_replies
               WHERE number = ? AND status = 'approved' AND final_text = ?
                 AND (draft_text IS NULL OR draft_text != final_text)""",
            (number, text),
        ).fetchone()
    return row is not None


@app.post("/outgoing")
def outgoing(msg: OutgoingMessage):
    source = msg.source
    if source == "bot" and was_written_by_owner(msg.number, msg.text):
        source = "owner_edit"  # your words -> they count as style examples
    db.save_message(msg.message_id, msg.chat_id, msg.number, "out", source,
                    msg.type, msg.text, msg.timestamp)
    return {"saved": True, "source": source}


@app.get("/outbox")
def outbox():
    return db.list_pending("approved")


@app.post("/outbox/{pending_id}/sent")
def outbox_sent(pending_id: int):
    item = db.mark_sent(pending_id)
    if not item:
        raise HTTPException(404, "No such item")
    return item


# ---------- endpoints for YOU ----------

@app.post("/test-draft")
def test_draft(body: TestDraft):
    """Try the AI without WhatsApp. Nothing is saved or sent."""
    contact = db.get_contact(body.number)
    if not contact:
        raise HTTPException(404, "Unknown contact. They must message you first, or import a chat export.")
    try:
        d = make_draft(contact, body.text)
    except Exception as err:
        raise HTTPException(502, f"AI error: {err}")
    prompt = d.pop("_prompt")
    if body.show_prompt:
        d["prompt"] = prompt
    return d


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
        raise HTTPException(404, "Contact not found.")
    return item


@app.get("/messages/{number}")
def messages(number: str, limit: int = 20):
    return db.recent_messages(number, limit)
