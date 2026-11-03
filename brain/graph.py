"""
graph.py — STEP 5: the brain as a LangGraph workflow.

LangGraph lets us draw the brain as a flowchart. Each box is a small Python function
(a "node"). They all share one dictionary (the "state"): each node reads what it needs
from it and writes its results back into it.

    START
      │
      ▼
  understand ── not text (voice, image...) ──────────────────┐
      │ text                                                 │
      ▼                                                      │
  identify    who is writing? have you replied to them?      │
      │                                                      │
      ▼                                                      │
  draft       AI writes a reply in your style ── AI error ───┤
      │                                                      │
      ▼                                                      │
  ground      number check + AI fact check                   │
      │                                                      │
      ▼                                                      ▼
  decide      reply automatically, or hold for you  ◄────────┘
      │
     END

Later steps plug into this same graph:
  Step 6 upgrades `identify` (what the person needs: intent, urgency)
  Step 7 upgrades `understand` (voice notes -> faster-whisper, images -> BLIP)
"""

import operator
import os
from datetime import datetime
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import db
import db_v5
import grounding
import llm
import style

AUTO_SEND_MIN_CONFIDENCE = float(os.getenv("AUTO_SEND_MIN_CONFIDENCE", "0.85"))


class AgentState(TypedDict, total=False):
    # --- input: one WhatsApp message ---
    message_id: Optional[str]
    number: str
    sender_name: str
    msg_type: str
    text: str
    # --- written by the nodes ---
    understood_text: Optional[str]   # understand
    contact: dict                    # identify
    human_replies: int               # identify
    is_new_contact: bool             # identify
    draft: Optional[dict]            # draft
    prompt: dict                     # draft (for /test-draft)
    error: Optional[str]             # draft
    grounding: Optional[dict]        # ground
    action: str                      # decide: reply / hold
    reply_text: Optional[str]        # decide
    reason: str                      # decide
    # every node adds one line here, so you can see the path the message took
    trace: Annotated[list[str], operator.add]


# ---------- nodes ----------

def understand(state: AgentState) -> dict:
    """Turn the message into text. Step 7 will add voice notes and images here."""
    text = (state.get("text") or "").strip()
    if state.get("msg_type", "chat") == "chat" and text:
        return {"understood_text": text, "trace": ["understand: text message"]}
    return {"understood_text": None,
            "trace": [f"understand: '{state.get('msg_type')}' message, can't read it yet (Step 7)"]}


def identify(state: AgentState) -> dict:
    """Who is writing? Step 6 will add: what do they need, how urgent is it."""
    contact = db.get_contact(state["number"])
    replies = db_v5.human_reply_count(state["number"])
    rel = contact["relationship"]
    line = (f"identify: {contact['display_name']} ({rel}), you replied yourself {replies} time(s), "
            f"auto_reply {'on' if contact['auto_reply'] == 1 else 'off'}")
    return {"contact": contact, "human_replies": replies, "is_new_contact": replies == 0, "trace": [line]}


DRAFT_SYSTEM_ADDON = """

KNOWN FACTS about {owner} and TODAY are given at the top of the user message.
You may state something about {owner}'s plans, availability, schedule or whereabouts as true ONLY if it is in KNOWN FACTS or the recent conversation. Otherwise set needs_owner to true."""


def draft(state: AgentState) -> dict:
    """The AI writes a reply in your style (same as Step 4, plus your owner facts and today's date)."""
    try:
        system, user, n_examples = style.build_prompts(state["contact"], state["understood_text"],
                                                       state.get("message_id"))
        system += DRAFT_SYSTEM_ADDON.format(owner=style.OWNER_NAME)
        user = (f"TODAY: {grounding.today_line()}\n\n"
                f"KNOWN FACTS ABOUT {style.OWNER_NAME.upper()} (true right now):\n{grounding.facts_block()}\n\n"
                + user)
        d = llm.draft_json(system, user)
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}"[:300], "draft": None,
                "trace": [f"draft: AI error ({type(err).__name__})"]}
    d["examples_used"] = n_examples
    line = (f"draft: {d['reply']!r} (confidence {d['confidence']:.2f}, "
            f"needs_owner {d['needs_owner']}, {n_examples} examples)")
    return {"draft": d, "prompt": {"system": system, "user": user}, "error": None, "trace": [line]}


def ground(state: AgentState) -> dict:
    """Check the draft against what we actually know."""
    g = grounding.check(state["draft"]["reply"], state["contact"], state["understood_text"],
                        state.get("message_id"))
    if g["error"]:
        line = f"ground: check could not run ({g['error'][:80]})"
    elif g["passed"]:
        line = f"ground: passed ({len(g['claims'])} claim(s), all supported)"
    else:
        bits = [f"unsupported {c!r}" for c in g["unsupported_claims"]]
        if g["unsupported_numbers"]:
            bits.append("unknown numbers " + ", ".join(g["unsupported_numbers"]))
        line = "ground: FAILED, " + "; ".join(bits)
    return {"grounding": g, "trace": [line]}


def decide_rules(state: AgentState) -> dict:
    """The final decision, as plain rules. Every rule that fails is listed as a reason."""
    if state.get("understood_text") is None:
        return {"action": "hold", "reply_text": None,
                "reason": f"'{state.get('msg_type')}' messages not supported yet"}
    if state.get("error"):
        return {"action": "hold", "reply_text": None, "reason": f"AI error: {state['error']}"}

    d, g, contact = state["draft"], state.get("grounding") or {}, state["contact"]
    problems = []
    if g.get("error"):
        problems.append(f"grounding check could not run ({g['error'][:120]})")
    for claim in g.get("unsupported_claims", []):
        problems.append(f"unsupported claim: \"{claim}\"")
    if g.get("unsupported_numbers"):
        problems.append("numbers not found in chat or facts: " + ", ".join(g["unsupported_numbers"]))
    if d["needs_owner"]:
        problems.append(f"needs you: {d['reason']}")
    if d["confidence"] < AUTO_SEND_MIN_CONFIDENCE:
        problems.append(f"low confidence ({d['confidence']:.2f} < {AUTO_SEND_MIN_CONFIDENCE})")
    if state.get("is_new_contact"):
        problems.append("you have never replied to this contact yourself")
    if contact["auto_reply"] != 1:
        problems.append("auto_reply is off for this contact")

    stats = f"[confidence {d['confidence']:.2f}, {d['examples_used']} examples]"
    if problems:
        return {"action": "hold", "reply_text": d["reply"], "reason": "; ".join(problems) + " " + stats}
    return {"action": "reply", "reply_text": d["reply"], "reason": "all checks passed " + stats}


def decide(state: AgentState) -> dict:
    result = decide_rules(state)
    word = "SEND" if result["action"] == "reply" else "HOLD"
    return {**result, "trace": [f"decide: {word} ({result['reason'][:150]})"]}


# ---------- wiring the flowchart ----------

def after_understand(state: AgentState) -> str:
    return "identify" if state.get("understood_text") else "decide"


def after_draft(state: AgentState) -> str:
    return "decide" if state.get("error") else "ground"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("understand", understand)
    g.add_node("identify", identify)
    g.add_node("draft", draft)
    g.add_node("ground", ground)
    g.add_node("decide", decide)

    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", after_understand, {"identify": "identify", "decide": "decide"})
    g.add_edge("identify", "draft")
    g.add_conditional_edges("draft", after_draft, {"ground": "ground", "decide": "decide"})
    g.add_edge("ground", "decide")
    g.add_edge("decide", END)
    return g.compile()


GRAPH = build_graph()


def run(number: str, sender_name: str, text: str, msg_type: str = "chat",
        message_id: str | None = None) -> dict:
    """Push one message through the graph and return the final state."""
    started = datetime.now()
    final = GRAPH.invoke({
        "message_id": message_id, "number": number, "sender_name": sender_name,
        "msg_type": msg_type, "text": text, "trace": [],
    })
    final["seconds"] = round((datetime.now() - started).total_seconds(), 2)
    return final


def mermaid() -> str:
    """The flowchart as Mermaid text (paste it into mermaid.live to see the picture)."""
    return GRAPH.get_graph().draw_mermaid()
