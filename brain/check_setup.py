"""
check_setup.py — run this after setup to see if everything is ready.

    python check_setup.py          offline checks: no WhatsApp, no OpenAI, no cost
    python check_setup.py --live   also makes ONE real OpenAI call (a fraction of a cent)

Your real database (data/agent.db) is never touched. All tests run on a
temporary database that is deleted at the end.
"""

import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Windows terminals sometimes can't print Bangla or emoji; this avoids crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
results: list[str] = []


def report(status: str, message: str) -> None:
    print(f"{status} {message}")
    results.append(status)


def check(condition: bool, ok_message: str, fail_message: str) -> None:
    report(OK if condition else FAIL, ok_message if condition else fail_message)


# ---------- 1. Python and packages ----------

def check_python() -> bool:
    v = sys.version_info
    if v < (3, 10):
        report(FAIL, f"Python {v.major}.{v.minor} found, but 3.10 or newer is needed (3.11 or 3.12 recommended)")
        return False
    report(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    if sys.prefix != sys.base_prefix:
        report(OK, f"running inside a virtual environment ({sys.prefix})")
    else:
        report(WARN, "not running inside brain/.venv. In VS Code: Ctrl+Shift+P > 'Python: Select Interpreter' > brain/.venv")
    return True


def check_packages() -> bool:
    ok = True
    packages = [(n, "requirements_step4.txt") for n in ("fastapi", "uvicorn", "pydantic", "openai", "dotenv")]
    packages.append(("httpx", "requirements_dev.txt"))  # only needed by this checker
    for name, req_file in packages:
        try:
            module = importlib.import_module(name)
            report(OK, f"package '{name}' {getattr(module, '__version__', '')}".rstrip())
        except ImportError:
            report(FAIL, f"package '{name}' is missing. Run: pip install -r {req_file}")
            ok = False
    return ok


def check_env() -> None:
    env = HERE / ".env"
    if not env.exists():
        report(WARN, ".env not found. Copy .env.example to .env and add your OpenAI key (needed for Step 4)")
        return
    from dotenv import dotenv_values
    values = dotenv_values(env)
    key = values.get("OPENAI_API_KEY") or ""
    if not key or "paste-your-key" in key:
        report(WARN, ".env exists, but OPENAI_API_KEY is still empty or the placeholder")
    else:
        report(OK, f".env has an API key (ends in ...{key[-4:]}), model = {values.get('OPENAI_MODEL') or '(default)'}")


# ---------- 2. Offline test of the whole Step 3 + 4 brain ----------

class FakeOpenAI:
    """Stands in for OpenAI so the test is free and works without internet."""

    def __init__(self):
        self.chat = self
        self.completions = self
        self.next_reply: dict | None = None

    def create(self, **kwargs):
        if self.next_reply is None:
            raise RuntimeError("simulated OpenAI outage")

        class Obj:
            pass

        response, choice = Obj(), Obj()
        choice.message = Obj()
        choice.message.content = json.dumps(self.next_reply)
        response.choices = [choice]
        return response


def offline_flow() -> None:
    warnings.filterwarnings("ignore")
    import db

    tmp = Path(tempfile.mkdtemp(prefix="wa_agent_check_"))
    real_path = db.DB_PATH
    db.DB_PATH = tmp / "check.db"
    try:
        # a) chat importer on the bundled sample file
        import import_chat_export as importer
        sample = HERE / "samples" / "sample_chat_android.txt"
        saved_argv = sys.argv
        sys.argv = ["import_chat_export.py", str(sample), "--me", "Stanley", "--number", "8801700000001"]
        try:
            importer.main()
        finally:
            sys.argv = saved_argv
        imported = db.recent_messages("8801700000001", 100)
        mine = [m for m in imported if m["source"] == "export"]
        check(len(imported) == 11 and len(mine) == 5,
              f"chat importer: {len(imported)} sample messages, {len(mine)} of them yours",
              f"chat importer: expected 11 messages (5 yours), got {len(imported)} ({len(mine)} yours)")

        # b) the brain, with a fake OpenAI
        import llm
        import main_v4
        from fastapi.testclient import TestClient

        fake = FakeOpenAI()
        llm._client = fake
        base = dict(chat_id="8801700000001@c.us", sender_name="Test Friend",
                    sender_number="8801700000001", timestamp=1791500000)
        confident = dict(reply="valo, tor?", language="banglish", confidence=0.95,
                         needs_owner=False, reason="simple greeting")

        with TestClient(main_v4.app) as client:
            fake.next_reply = confident
            r = client.post("/incoming", json=dict(base, message_id="c1", text="ki obostha?")).json()
            pend = client.get("/pending").json()
            check(r["action"] == "hold" and pend and pend[-1]["draft_text"] == "valo, tor?",
                  "auto_reply off (default) -> draft held for your approval",
                  f"expected hold with a draft, got {r}")

            r = client.post("/incoming", json=dict(base, message_id="c1", text="ki obostha?")).json()
            check(r["action"] == "ignore", "duplicate WhatsApp message -> ignored",
                  f"duplicate should be ignored, got {r}")

            client.patch("/contacts/8801700000001", json={"auto_reply": 1, "relationship": "friend"})
            r = client.post("/incoming", json=dict(base, message_id="c2", text="kire")).json()
            check(r["action"] == "reply", "auto_reply on + confident -> sends automatically",
                  f"expected reply, got {r}")

            fake.next_reply = dict(confident, reply="hae free", needs_owner=True, reason="availability")
            r = client.post("/incoming", json=dict(base, message_id="c3", text="friday free?")).json()
            check(r["action"] == "hold", "reply depends on your plans -> held, even with auto_reply on",
                  f"expected hold, got {r}")

            fake.next_reply = dict(confident, confidence=0.4)
            r = client.post("/incoming", json=dict(base, message_id="c4", text="?")).json()
            check(r["action"] == "hold", "low confidence -> held", f"expected hold, got {r}")

            r = client.post("/incoming", json=dict(base, message_id="c5", type="ptt")).json()
            check(r["action"] == "hold", "voice note -> held (not supported until Step 7)",
                  f"expected hold, got {r}")

            fake.next_reply = None
            r = client.post("/incoming", json=dict(base, message_id="c6", text="hello")).json()
            check(r["action"] == "hold" and "AI error" in r["reason"],
                  "OpenAI failure -> held, never guessed", f"expected hold on AI error, got {r}")

            pid = client.get("/pending").json()[0]["id"]
            client.post(f"/pending/{pid}/approve", json={"text": "alhamdulillah, tor?"})
            outbox = client.get("/outbox").json()
            check(any(item["final_text"] == "alhamdulillah, tor?" for item in outbox),
                  "approve with your own edit -> appears in the outbox for the bridge to send",
                  f"edited approval not in outbox: {outbox}")

            r = client.post("/outgoing", json=dict(message_id="o1", chat_id=base["chat_id"],
                                                   number=base["sender_number"], text="alhamdulillah, tor?",
                                                   timestamp=1791500100, source="bot")).json()
            check(r.get("source") == "owner_edit", "your edited text is saved as a new style example",
                  f"expected source owner_edit, got {r}")

            fake.next_reply = confident
            r = client.post("/test-draft", json={"number": "8801700000001", "text": "kheyecho?"}).json()
            check(r.get("examples_used", 0) > 0,
                  f"/test-draft builds a prompt with {r.get('examples_used')} style examples",
                  f"/test-draft returned {r}")
    except Exception as err:  # anything unexpected is a failure, with the reason
        report(FAIL, f"offline test crashed: {type(err).__name__}: {err}")
    finally:
        try:
            import llm
            llm._client = None
        except Exception:
            pass
        db.DB_PATH = real_path
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 3. Optional: one real OpenAI call ----------

def live_call() -> None:
    import llm
    llm._client = None
    try:
        out = llm.draft_json(
            "You draft short WhatsApp replies. Answer in the required JSON format.",
            "NEW MESSAGE FROM Test Friend\nki obostha?\n\nWrite the reply.",
        )
        report(OK, f"real OpenAI call works with {llm.MODEL}: {out['reply']!r} (confidence {out['confidence']:.2f})")
    except Exception as err:
        text = str(err)
        hint = ""
        if "model" in text and ("not found" in text or "does not exist" in text):
            hint = " -> change OPENAI_MODEL in .env"
        elif "reasoning_effort" in text:
            hint = " -> your model doesn't support reasoning_effort; use a gpt-5.x mini model"
        elif "quota" in text or "billing" in text:
            hint = " -> add credit at platform.openai.com > Billing"
        elif "API key" in text or "401" in text or "missing" in text:
            hint = " -> check OPENAI_API_KEY in .env"
        report(FAIL, f"real OpenAI call failed: {text[:200]}{hint}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check that the WhatsApp agent brain is set up correctly.")
    parser.add_argument("--live", action="store_true", help="also make one real OpenAI call")
    args = parser.parse_args()

    print("\n=== 1. Python and packages ===")
    if check_python() and check_packages():
        check_env()
        print("\n=== 2. Offline test of the brain (fake OpenAI, temporary database) ===")
        offline_flow()
        if args.live:
            print("\n=== 3. Real OpenAI call ===")
            live_call()

    fails, warns = results.count(FAIL), results.count(WARN)
    print("\n" + "=" * 60)
    if fails:
        print(f"{fails} problem(s) found. Fix the [FAIL] lines above and run this again.")
    elif warns:
        print(f"Code works. {warns} thing(s) still to set up (see [WARN] lines).")
    else:
        print("Everything is ready.")
    if not args.live:
        print("Tip: run 'python check_setup.py --live' once your .env has an API key.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
