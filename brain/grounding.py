"""
grounding.py — STEP 5: check a draft BEFORE it can be sent.

"Grounded" means: everything the draft claims is backed by something we actually know.
The bot may know things from only four places (the EVIDENCE):
    1. the conversation with this person in the last 48 hours (EVIDENCE_HOURS)
    2. your notes about this contact
    3. your owner facts (POST /facts), e.g. "Free on Friday evening"
    4. today's date and time
Your old style examples are NOT evidence: "hae 5 tay" last month says nothing about today.

Two independent checks (a draft must pass BOTH):

  A) number check (plain Python, no AI): every number in the draft (times, dates, prices...)
     must appear somewhere in the evidence. Bangla digits are understood too (৫ = 5).
     Cheap, fast, and it can't be fooled the way an AI can.

  B) AI fact check: a second, separate OpenAI call reads the draft as a strict fact-checker,
     lists every claim or promise, and marks each one supported / not supported.
     Example it must catch: "friday free?" -> draft "hae free" (the bot can't know that).
"""

import os
import re
from datetime import datetime

import db
import db_v5
import llm_v5
import style

# Only messages from the last N hours count as evidence (old chats say nothing about today).
# Change it in .env with EVIDENCE_HOURS=24 (for example).
EVIDENCE_HOURS = float(os.getenv("EVIDENCE_HOURS", "48"))

# ---------- shared: what do we actually know? ----------

BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
NUMBER_RE = re.compile(r"\d+(?:[.:]\d+)*")


def today_line(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%A, %d %B %Y, %H:%M")


def facts_block(now: datetime | None = None) -> str:
    facts = db_v5.list_facts(today=(now or datetime.now()).date())
    if not facts:
        return "(none)"
    lines = []
    for f in facts:
        until = f" (valid until {f['valid_until']})" if f["valid_until"] else ""
        lines.append(f"- {f['text']}{until}")
    return "\n".join(lines)


def conversation_block(contact: dict, new_text: str, message_id: str | None,
                       now: datetime | None = None) -> str:
    since = (now or datetime.now()).timestamp() - EVIDENCE_HOURS * 3600
    recent = [m for m in db.recent_messages(contact["number"], style.RECENT_CHAT_SIZE + 1)
              if m["wa_message_id"] != message_id and m["timestamp"] >= since][-style.RECENT_CHAT_SIZE:]
    lines = [] if recent else [f"(no messages in the last {EVIDENCE_HOURS:g} hours)"]
    for m in recent:
        who = contact["display_name"] if m["direction"] == "in" else style.OWNER_NAME
        lines.append(f"{who}: {style._clip(m['text']) or '(' + m['type'] + ')'}")
    lines.append(f"{contact['display_name']} (NEW MESSAGE): {new_text}")
    return "\n".join(lines)


def evidence_text(contact: dict, new_text: str, message_id: str | None,
                  now: datetime | None = None) -> str:
    owner = style.OWNER_NAME
    return "\n".join([
        f"TODAY: {today_line(now)}",
        "",
        f"KNOWN FACTS ABOUT {owner.upper()} (true right now):",
        facts_block(now),
        "",
        f"CONTACT: {contact['display_name']} | relationship: {contact['relationship']} "
        f"| notes: {contact['notes'] or '-'}",
        "",
        f"RECENT CONVERSATION (last {EVIDENCE_HOURS:g} hours, oldest first):",
        conversation_block(contact, new_text, message_id, now),
    ])


# ---------- A) number check ----------

def _normalize_number(token: str) -> str:
    parts = re.split(r"[.:]", token)
    return ":".join(p.lstrip("0") or "0" for p in parts)


def numbers_in(text: str) -> list[str]:
    """All numbers in a text, normalized: '০৫:৩০' -> '5:30', '5.30' -> '5:30', '05' -> '5'."""
    return [_normalize_number(t) for t in NUMBER_RE.findall(text.translate(BANGLA_DIGITS))]


def number_check(draft_reply: str, evidence: str) -> list[str]:
    """Returns the numbers in the draft that appear nowhere in the evidence (empty = OK)."""
    known = set()
    for token in numbers_in(evidence):
        known.add(token)
        known.update(token.split(":"))  # '5:30' in evidence also vouches for '5' and '30'
    missing = []
    for token in numbers_in(draft_reply):
        if token not in known and token not in missing:
            missing.append(token)
    return missing


# ---------- B) AI fact check ----------

GROUNDING_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "kind": {"type": "string", "enum": [
                        "plan_or_availability", "promise", "location", "time_or_date",
                        "number_or_money", "fact_about_someone", "answer", "opinion", "other",
                    ]},
                    "supported": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["claim", "kind", "supported", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["claims", "summary"],
    "additionalProperties": False,
}

GROUNDING_SYSTEM = """You are a strict fact-checker. Another AI drafted a WhatsApp reply that will be sent as if {owner} wrote it. Find every claim or commitment in the DRAFT and check whether the EVIDENCE supports it. Do not judge style, tone or language.

List as a claim:
- anything about {owner}'s plans, availability, schedule, location, health, or actions done / not done
- promises and commitments: agreeing to come, meet, pay, send, call, help; saying yes or no to a request or invitation
- times, dates, numbers, prices, places, names
- facts about other people
- answers to factual questions
- strong opinions on serious matters

Do NOT list: greetings, thanks, emojis, laughter, asking a question back, saying {owner} will check or reply later, friendly chit-chat with no factual content.

A claim is supported ONLY if the KNOWN FACTS, the CONTACT notes, the RECENT CONVERSATION or TODAY clearly state it or it follows directly from them. If the evidence does not say it, it is NOT supported, even if it sounds likely. If the new message asks a yes/no question about {owner}'s plans or availability, a yes or no in the draft is a claim.

For each claim, "evidence" quotes the supporting line, or says "no evidence". Return an empty list if the draft makes no claims. "summary" is one short sentence."""


def ai_fact_check(draft_reply: str, evidence: str) -> dict:
    owner = style.OWNER_NAME
    user_prompt = f"{evidence}\n\nDRAFT REPLY (written by an AI, not sent yet):\n{draft_reply}\n\nCheck the draft."
    return llm_v5.json_call(
        GROUNDING_SYSTEM.format(owner=owner), user_prompt,
        GROUNDING_SCHEMA, "grounding_check", model=llm_v5.GROUNDING_MODEL,
    )


# ---------- both together ----------

def check(draft_reply: str, contact: dict, new_text: str, message_id: str | None = None,
          now: datetime | None = None) -> dict:
    """
    Returns:
      passed               True only if BOTH checks pass and nothing went wrong
      unsupported_numbers  numbers in the draft we have no evidence for
      unsupported_claims   claims the AI fact-checker could not back up
      claims               everything the fact-checker listed
      summary              the fact-checker's one-line summary
      error                set if the AI fact check could not run (then passed = False)
    """
    evidence = evidence_text(contact, new_text, message_id, now)
    result = {
        "passed": False,
        "unsupported_numbers": number_check(draft_reply, evidence),
        "unsupported_claims": [],
        "claims": [],
        "summary": "",
        "error": None,
    }
    try:
        ai = ai_fact_check(draft_reply, evidence)
    except Exception as err:  # no key, timeout... -> the check did NOT pass
        result["error"] = f"{type(err).__name__}: {err}"[:300]
        return result

    result["claims"] = ai.get("claims", [])
    result["summary"] = ai.get("summary", "")
    result["unsupported_claims"] = [c["claim"] for c in result["claims"] if not c.get("supported")]
    result["passed"] = not result["unsupported_numbers"] and not result["unsupported_claims"]
    return result
