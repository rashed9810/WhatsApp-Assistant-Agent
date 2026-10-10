"""
main.py — STEP 2: the "brain" skeleton.

The Node bridge sends every incoming WhatsApp message here (POST /incoming).
The brain answers with one of three decisions:
    reply  -> send this text back
    hold   -> don't send; Stanley must decide (later this goes to the dashboard)
    ignore -> do nothing

Right now the rule is fixed and simple. In later steps, this function is
replaced by the LangGraph workflow (understand -> who/what -> draft -> check).

Run:  uvicorn main:app --reload --port 8000
Test without WhatsApp: open http://127.0.0.1:8000/docs in your browser.
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="WhatsApp Agent Brain")


class IncomingMessage(BaseModel):
    """What the Node bridge sends us. Must match the payload in 02_bridge_to_brain.js."""
    message_id: str
    chat_id: str
    sender_name: str
    sender_number: str
    type: str = "chat"
    text: str = ""
    has_media: bool = False
    timestamp: int


class BrainDecision(BaseModel):
    """What we send back to the Node bridge."""
    action: Literal["reply", "hold", "ignore"]
    text: Optional[str] = None
    reason: str = ""


@app.get("/health")
def health():
    """Quick check that the brain is running."""
    return {"status": "ok"}


@app.post("/incoming", response_model=BrainDecision)
def incoming(msg: IncomingMessage) -> BrainDecision:
    when = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
    print(f"[{when}] {msg.sender_name} ({msg.sender_number}) [{msg.type}]: {msg.text}")

    # Voice notes, images, stickers etc. are not understood yet (that's Step 7).
    # The rule from day one: if we don't understand it, we HOLD, we don't guess.
    if msg.type != "chat" or not msg.text.strip():
        return BrainDecision(action="hold", reason=f"'{msg.type}' messages not supported yet")

    # Placeholder reply. Later: LangGraph + OpenAI + grounding check.
    preview = msg.text[:50]
    return BrainDecision(
        action="reply",
        text=f'Brain received: "{preview}"',
        reason="fixed test reply",
    )
