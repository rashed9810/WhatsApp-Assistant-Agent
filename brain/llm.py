"""
llm.py — STEP 4: the only file that talks to OpenAI.

We ask the model to answer in a fixed JSON shape ("structured output"), so the
brain never has to guess what the AI meant:

    {
      "reply":       the WhatsApp message to send (in your style),
      "language":    bangla / english / banglish,
      "confidence":  0.0 - 1.0, how sure the AI is that YOU would send exactly this,
      "needs_owner": true if the reply depends on something only you know,
      "reason":      one short sentence explaining the confidence
    }
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
TIMEOUT_SECONDS = 20

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "language": {"type": "string", "enum": ["bangla", "english", "banglish"]},
        "confidence": {"type": "number"},
        "needs_owner": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["reply", "language", "confidence", "needs_owner", "reason"],
    "additionalProperties": False,
}

_client = None


def get_client() -> OpenAI:
    """Create the OpenAI client once (it reads OPENAI_API_KEY from .env)."""
    global _client
    if _client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is missing. Copy .env.example to .env and add your key.")
        _client = OpenAI(timeout=TIMEOUT_SECONDS, max_retries=1)
    return _client


def draft_json(system_prompt: str, user_prompt: str) -> dict:
    """Send the prompts to OpenAI and get back a dict matching DRAFT_SCHEMA."""
    extra = {}
    # Newer reasoning models (gpt-5.x, o-series) "think" before answering.
    # For short chat replies, low effort is faster and cheaper.
    if MODEL.startswith(("gpt-5", "o")):
        extra["reasoning_effort"] = "low"

    response = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "whatsapp_draft", "strict": True, "schema": DRAFT_SCHEMA},
        },
        **extra,
    )
    data = json.loads(response.choices[0].message.content)
    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))  # keep it between 0 and 1
    return data
