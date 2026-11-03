"""
llm_v5.py — STEP 5: a general "ask OpenAI for JSON" helper.

llm.py (Step 4) can only return the draft shape. The grounding check needs a different
shape, so this file adds one function that works with ANY JSON schema. It reuses the
same OpenAI client, key and settings as llm.py (llm.py itself is unchanged).

Optional setting in .env:
    GROUNDING_MODEL=...   use a different (e.g. stronger) model for the fact check.
                          Default: the same model as OPENAI_MODEL.
"""

import json
import os

import llm

GROUNDING_MODEL = os.getenv("GROUNDING_MODEL") or llm.MODEL


def json_call(system_prompt: str, user_prompt: str, schema: dict, schema_name: str,
              model: str | None = None) -> dict:
    """Send prompts to OpenAI and get back a dict that matches `schema` exactly."""
    model = model or llm.MODEL
    extra = {}
    if model.startswith(("gpt-5", "o")):
        extra["reasoning_effort"] = "low"

    response = llm.get_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        **extra,
    )
    return json.loads(response.choices[0].message.content)
