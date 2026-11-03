"""
check_step5.py — test the whole Step 5 brain on your computer, for free.

    python check_step5.py          offline: fake OpenAI, temporary database, no WhatsApp, no cost
    python check_step5.py --live   also runs ONE real message through the real graph
                                   (two OpenAI calls: draft + fact check, a fraction of a cent)

Your real database (data/agent.db) is never touched.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, FAIL = "[ OK ]", "[FAIL]"
results: list[str] = []


def check(condition: bool, ok_message: str, fail_message: str) -> None:
    status = OK if condition else FAIL
    print(f"{status} {ok_message if condition else fail_message}")
    results.append(status)


class FakeOpenAI:
    """Plays both AIs. It looks at which JSON shape is requested to know which role to play."""

    def __init__(self):
        self.chat = self
        self.completions = self
        self.next = {"whatsapp_draft": None, "grounding_check": None}
        self.calls = {"whatsapp_draft": 0, "grounding_check": 0}
        self.last_prompt = {}

    def create(self, **kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        self.calls[name] += 1
        self.last_prompt[name] = kwargs["messages"][0]["content"] + "\n" + kwargs["messages"][1]["content"]
        answer = self.next[name]
        if answer is None:
            raise RuntimeError(f"simulated OpenAI outage ({name})")

        class Obj:
            pass

        response, choice = Obj(), Obj()
        choice.message = Obj()
        choice.message.content = json.dumps(answer)
        response.choices = [choice]
        return response


def draft(reply, confidence=0.95, needs_owner=False, reason="test"):
    return {"reply": reply, "language": "banglish", "confidence": confidence,
            "needs_owner": needs_owner, "reason": reason}


def claims(*items):
    """items: (claim text, supported True/False)"""
    return {"claims": [{"claim": c, "kind": "plan_or_availability", "supported": s,
                        "evidence": "quoted line" if s else "no evidence"} for c, s in items],
            "summary": "test"}


def offline() -> None:
    warnings.filterwarnings("ignore")
    try:
        import langgraph  # noqa: F401
    except ImportError:
        check(False, "", "LangGraph is not installed. Run: pip install -r requirements_step5.txt")
        return

    import db
    tmp = Path(tempfile.mkdtemp(prefix="wa_agent_step5_"))
    real_path = db.DB_PATH
    db.DB_PATH = tmp / "check.db"
    try:
        import import_chat_export as importer
        import grounding
        import llm
        import main_v5
        from fastapi.testclient import TestClient

        # A friend you have chatted with before (imported sample chat, from October)
        friend = "8801700000001"
        saved_argv = sys.argv
        sys.argv = ["import_chat_export.py", str(HERE / "samples" / "sample_chat_android.txt"),
                    "--me", "Stanley", "--number", friend]
        try:
            importer.main()
        finally:
            sys.argv = saved_argv

        fake = FakeOpenAI()
        llm._client = fake
        counter = {"n": 0}

        def send(client, text, number=friend, name="Test Friend", type_="chat"):
            counter["n"] += 1
            return client.post("/incoming", json={
                "message_id": f"s5-{counter['n']}", "chat_id": f"{number}@c.us", "sender_name": name,
                "sender_number": number, "type": type_, "text": text, "timestamp": int(time.time())}).json()

        with TestClient(main_v5.app) as client:
            client.patch(f"/contacts/{friend}", json={"auto_reply": 1, "relationship": "friend"})

            print("\n--- Normal cases ---")
            fake.next.update(whatsapp_draft=draft("alhamdulillah valo, tor?"), grounding_check=claims())
            r = send(client, "ki obostha?")
            check(r["action"] == "reply" and r["text"] == "alhamdulillah valo, tor?",
                  "greeting, no claims, auto_reply on -> sent automatically", f"expected reply, got {r}")

            print("\n--- What Step 5 is for: the drafting AI is over-confident ---")
            fake.next.update(whatsapp_draft=draft("hae free 😄", confidence=0.95),
                             grounding_check=claims(("Stanley is free on Friday", False)))
            r = send(client, "friday free?")
            check(r["action"] == "hold" and "unsupported claim" in r["reason"],
                  "'friday free?' -> 'hae free' -> fact check finds no evidence -> HELD",
                  f"expected hold for unsupported claim, got {r}")
            pend = client.get("/pending").json()
            check(bool(pend) and pend[-1]["draft_text"] == "hae free 😄",
                  "the draft is waiting in the approval queue for you",
                  f"draft not in queue: {pend}")

            print("\n--- Owner facts let the bot answer safely ---")
            future = (date.today() + timedelta(days=3)).isoformat()
            fact = client.post("/facts", json={"text": "Free on Friday evening", "valid_until": future}).json()
            fake.next.update(whatsapp_draft=draft("hae, sondhay free"),
                             grounding_check=claims(("Stanley is free Friday evening", True)))
            r = send(client, "friday free?")
            check(r["action"] == "reply", "with the fact 'Free on Friday evening' -> sent",
                  f"expected reply, got {r}")
            check("Free on Friday evening" in fake.last_prompt["whatsapp_draft"]
                  and "Free on Friday evening" in fake.last_prompt["grounding_check"],
                  "your facts reach BOTH the drafting AI and the fact checker",
                  "fact missing from one of the prompts")
            check("hae 5 tay" not in fake.last_prompt["grounding_check"],
                  "old imported chats are NOT used as evidence (only the last 48 hours)",
                  "old chat lines leaked into the grounding evidence")

            print("\n--- The number check catches what the fact checker misses ---")
            fake.next.update(whatsapp_draft=draft("730 taka lagbe"), grounding_check=claims())
            r = send(client, "koto lagbe?")
            check(r["action"] == "hold" and "730" in r["reason"],
                  "draft says '730 taka' with no evidence -> HELD, even though the AI checker said OK",
                  f"expected hold for unknown number, got {r}")
            fake.next.update(whatsapp_draft=draft("ok 730 e done"), grounding_check=claims())
            r = send(client, "730 taka dile hobe?")
            check(r["action"] == "reply", "same number, but it's in their message -> allowed",
                  f"expected reply, got {r}")

            print("\n--- Other reasons to hold ---")
            fake.next.update(whatsapp_draft=draft("ok", needs_owner=True, reason="money"), grounding_check=claims())
            r = send(client, "taka ta pathiye dis")
            check(r["action"] == "hold" and "needs you" in r["reason"], "drafting AI says it needs you -> HELD",
                  f"expected hold, got {r}")

            fake.next.update(whatsapp_draft=draft("hmm", confidence=0.5), grounding_check=claims())
            r = send(client, "?")
            check(r["action"] == "hold" and "low confidence" in r["reason"], "low confidence -> HELD",
                  f"expected hold, got {r}")

            fake.next.update(whatsapp_draft=draft("valo"), grounding_check=None)
            r = send(client, "kemon acho")
            check(r["action"] == "hold" and "could not run" in r["reason"],
                  "fact checker fails (outage) -> HELD, never sent unchecked", f"expected hold, got {r}")

            before = dict(fake.calls)
            fake.next.update(whatsapp_draft=None, grounding_check=claims())
            r = send(client, "hello")
            check(r["action"] == "hold" and "AI error" in r["reason"]
                  and fake.calls["grounding_check"] == before["grounding_check"],
                  "drafting AI fails -> HELD, and the fact check is skipped", f"got {r}")

            before = dict(fake.calls)
            r = send(client, "", type_="ptt")
            check(r["action"] == "hold" and fake.calls == before,
                  "voice note -> HELD without any AI call (Step 7 will read it)", f"got {r}")

            stranger = "8801700000009"
            fake.next.update(whatsapp_draft=draft("hi"), grounding_check=claims())
            send(client, "hi", number=stranger, name="Stranger")
            client.patch(f"/contacts/{stranger}", json={"auto_reply": 1})
            r = send(client, "hello?", number=stranger, name="Stranger")
            check(r["action"] == "hold" and "never replied" in r["reason"],
                  "auto_reply on, but you have never replied to them yourself -> HELD",
                  f"expected hold, got {r}")

            dup = client.post("/incoming", json={"message_id": "s5-1", "chat_id": f"{friend}@c.us",
                                                 "sender_name": "Test Friend", "sender_number": friend,
                                                 "text": "ki obostha?", "timestamp": int(time.time())}).json()
            check(dup["action"] == "ignore", "duplicate WhatsApp message -> ignored", f"got {dup}")

            print("\n--- Tools for you ---")
            n_pending = len(client.get("/pending").json())
            n_logged = len(client.get("/decisions?limit=500").json())
            fake.next.update(whatsapp_draft=draft("valo"), grounding_check=claims())
            t = client.post("/test-draft", json={"number": friend, "text": "kemon acho"}).json()
            steps = [line.split(":")[0] for line in t.get("trace", [])]
            check(steps == ["understand", "identify", "draft", "ground", "decide"] and t["would"] == "SEND",
                  "/test-draft walks all 5 graph steps: " + " -> ".join(steps), f"/test-draft returned {t}")
            check(len(client.get("/pending").json()) == n_pending
                  and len(client.get("/decisions?limit=500").json()) == n_logged,
                  "/test-draft saves nothing", "/test-draft changed the database")

            log = client.get("/decisions?limit=1").json()
            check(bool(log) and log[0]["trace"] and "action" in log[0],
                  f"/decisions log works ({n_logged} decisions recorded)", f"log: {log}")
            check("ground" in client.get("/graph").text, "/graph returns the flowchart", "/graph failed")

            past = (date.today() - timedelta(days=1)).isoformat()
            client.post("/facts", json={"text": "Was in Sylhet", "valid_until": past})
            active = [f["text"] for f in client.get("/facts").json()]
            bad = client.post("/facts", json={"text": "x", "valid_until": "next friday"}).status_code
            deleted = client.delete(f"/facts/{fact['id']}").status_code
            check("Was in Sylhet" not in active and "Free on Friday evening" in active
                  and bad == 400 and deleted == 200,
                  "facts: expired ones ignored, bad dates refused, delete works",
                  f"facts problem: active={active}, bad date -> {bad}, delete -> {deleted}")

            pid = client.get("/pending").json()[0]["id"]
            client.post(f"/pending/{pid}/approve", json={"text": "ami pore janacchi"})
            out = client.post("/outgoing", json={"message_id": "o1", "chat_id": f"{friend}@c.us",
                                                 "number": friend, "text": "ami pore janacchi",
                                                 "timestamp": int(time.time()), "source": "bot"}).json()
            check(out.get("source") == "owner_edit", "Step 4 behavior kept: your edits become style examples",
                  f"got {out}")

        check(grounding.numbers_in("৫টায় আসব") == ["5"], "Bangla digits understood (৫ = 5)",
              f"got {grounding.numbers_in('৫টায় আসব')}")
    except Exception as err:
        import traceback
        traceback.print_exc()
        check(False, "", f"test crashed: {type(err).__name__}: {err}")
    finally:
        try:
            import llm
            llm._client = None
        except Exception:
            pass
        db.DB_PATH = real_path
        shutil.rmtree(tmp, ignore_errors=True)


def live() -> None:
    """One real message through the real graph, on a temporary database."""
    import db
    tmp = Path(tempfile.mkdtemp(prefix="wa_agent_step5_live_"))
    real_path = db.DB_PATH
    db.DB_PATH = tmp / "live.db"
    try:
        import db_v5
        import graph
        import llm
        llm._client = None
        db_v5.init_db_v5()
        db.upsert_contact("8801700000001", "8801700000001@c.us", "Test Friend")
        state = graph.run("8801700000001", "Test Friend", "friday free? cricket khelbi?")
        for line in state["trace"]:
            print("       " + line)
        g = state.get("grounding") or {}
        problem = state.get("error") or g.get("error")
        check(not problem, f"real draft + real fact check both work ({state.get('seconds')} s)",
              f"real OpenAI call failed: {problem}")
        if not problem:
            d = state["draft"]
            print(f"       draft: {d['reply']!r}  (needs_owner={d['needs_owner']})")
            if g["unsupported_claims"]:
                print(f"       fact check: caught {g['unsupported_claims']}  <- good, it can't know your plans")
            else:
                print("       fact check: no unsupported claims. Read the draft: it should NOT promise "
                      "anything about Friday. If it does, tell Claude.")
    finally:
        db.DB_PATH = real_path
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the Step 5 brain.")
    parser.add_argument("--live", action="store_true", help="also run one message with real OpenAI calls")
    args = parser.parse_args()

    print("=== Step 5 offline test (fake OpenAI, temporary database) ===")
    offline()
    if args.live:
        print("\n=== Real OpenAI test ===")
        live()

    fails = results.count(FAIL)
    print("\n" + "=" * 60)
    print(f"{len(results) - fails}/{len(results)} checks passed." if not fails
          else f"{fails} check(s) failed. Copy the output and send it to Claude.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
