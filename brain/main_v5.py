"""
main_v5.py — STEP 5 brain: LangGraph workflow + grounding check.

Same endpoints as main_v4.py (so the Step 3 bridge works unchanged), but every incoming
message now goes through the graph in graph.py:

    understand -> identify -> draft -> ground -> decide

New endpoints:
    GET/POST/DELETE /facts   things that are true about you now ("Free on Friday evening")
    GET /decisions           what the brain decided for recent messages, and why
    GET /graph               the flowchart as Mermaid text

Auto-send now needs ALL of these:
    auto_reply = 1 for the contact          you have replied to this contact yourself before
    the AI did not set needs_owner          confidence >= AUTO_SEND_MIN_CONFIDENCE
    number check passed                     AI fact check passed (no unsupported claims)

Run:  python run_step5.py      (or: uvicorn main_v5:app --reload --port 8000)
"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import db
import db_v5
import graph
import llm
import llm_v5
import style


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_v5.init_db_v5()
    print(f"Database ready at {db.DB_PATH}")
    print(f"Draft model: {llm.MODEL} | grounding model: {llm_v5.GROUNDING_MODEL} | owner: {style.OWNER_NAME}")
    print(f"Auto-send needs confidence >= {graph.AUTO_SEND_MIN_CONFIDENCE} and a passed grounding check")
    print(f"Active owner facts: {len(db_v5.list_facts())}")
    yield


app = FastAPI(title="WhatsApp Agent Brain (Step 5: LangGraph + grounding)", lifespan=lifespan)


# ---------- data shapes ----------

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
    number: str                # a contact that already exists (see GET /contacts)
    text: str                  # pretend they just sent this
    show_prompt: bool = False  # True = also show the exact prompt sent to the drafting AI


class FactIn(BaseModel):
    text: str                          # e.g. "Free on Friday evening"
    valid_until: Optional[str] = None  # e.g. "2026-11-07"; leave empty for no end date


# ---------- endpoints used by the bridge ----------

@app.get("/health")
def health():
    return {"status": "ok", "step": 5, "model": llm.MODEL, "grounding_model": llm_v5.GROUNDING_MODEL,
            "pending": len(db.list_pending("pending")), "facts": len(db_v5.list_facts())}


@app.post("/incoming", response_model=BrainDecision)
def incoming(msg: IncomingMessage) -> BrainDecision:
    when = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
    print(f"[{when}] IN  {msg.sender_name} ({msg.sender_number}) [{msg.type}]: {msg.text}")

    db.upsert_contact(msg.sender_number, msg.chat_id, msg.sender_name)
    is_new = db.save_message(msg.message_id, msg.chat_id, msg.sender_number, "in", "contact",
                             msg.type, msg.text, msg.timestamp)
    if not is_new:
        return BrainDecision(action="ignore", reason="duplicate message, already handled")

    try:
        state = graph.run(msg.sender_number, msg.sender_name, msg.text, msg.type, msg.message_id)
    except Exception as err:  # a bug anywhere in the graph -> hold, never guess
        state = {"message_id": msg.message_id, "number": msg.sender_number, "text": msg.text,
                 "action": "hold", "reply_text": None,
                 "reason": f"brain error: {type(err).__name__}: {err}"[:300],
                 "trace": [f"graph crashed: {type(err).__name__}"]}

    for line in state.get("trace", []):
        print(f"           {line}")
    db_v5.log_decision(state)

    if state["action"] == "reply":
        return BrainDecision(action="reply", text=state["reply_text"], reason=state["reason"])

    pid = db.add_pending(msg.chat_id, msg.sender_number, msg.message_id, state.get("reply_text"), state["reason"])
    print(f"           -> HELD as pending #{pid}")
    return BrainDecision(action="hold", reason=state["reason"])


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
    """Run the whole graph WITHOUT WhatsApp. Nothing is saved or sent."""
    contact = db.get_contact(body.number)
    if not contact:
        raise HTTPException(404, "Unknown contact. They must message you first, or import a chat export.")
    state = graph.run(body.number, contact["display_name"], body.text, "chat", None)
    result = {
        "would": "SEND" if state["action"] == "reply" else "HOLD",
        "reply": state.get("reply_text"),
        "reason": state["reason"],
        "trace": state["trace"],
        "draft": state.get("draft"),
        "grounding": state.get("grounding"),
        "seconds": state.get("seconds"),
    }
    if body.show_prompt:
        result["prompt"] = state.get("prompt")
    return result


@app.get("/facts")
def facts(include_expired: bool = False):
    return db_v5.list_facts(include_expired=include_expired)


@app.post("/facts")
def add_fact(body: FactIn):
    if not body.text.strip():
        raise HTTPException(400, "Write the fact in 'text'.")
    try:
        return db_v5.add_fact(body.text, body.valid_until)
    except ValueError:
        raise HTTPException(400, "valid_until must look like 2026-11-07 (year-month-day), or be left out.")


@app.delete("/facts/{fact_id}")
def remove_fact(fact_id: int):
    if not db_v5.delete_fact(fact_id):
        raise HTTPException(404, "No fact with that id.")
    return {"deleted": fact_id}


@app.get("/decisions")
def decisions(limit: int = 20, number: Optional[str] = None):
    return db_v5.recent_decisions(limit, number)


@app.get("/graph", response_class=PlainTextResponse)
def show_graph():
    return graph.mermaid()


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
