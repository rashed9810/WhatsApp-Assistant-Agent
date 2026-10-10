"""
import_chat_export.py — STEP 4: load an exported WhatsApp chat (.txt) into the notebook,
so the AI has real examples of how you reply, from day one.

How to export: open a chat on your phone -> ⋮ (Android) or contact name (iPhone)
-> More -> Export chat -> WITHOUT media. Send the .txt to your computer.

Usage (inside the brain folder, with .venv active):

  1) See who is in the file and how your name appears:
       python import_chat_export.py "WhatsApp Chat with Rahim.txt" --list-senders

  2) Import it:
       python import_chat_export.py "WhatsApp Chat with Rahim.txt" --me "Stanley" --number 8801712345678

     --me      your name exactly as it appears in the file
     --number  the contact's number, same format as in config.js / GET /contacts
     --mdy     add this if your phone shows dates as month/day (US style). Default is day/month.

Re-importing the same file is safe: messages already imported are skipped.
"""

import argparse
import hashlib
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import db

INVISIBLE = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202c\ufeff"), None)

# Android: 06/10/2026, 21:15 - Rahim: text      (also "9:15 pm")
# iPhone:  [06/10/26, 9:15:32 PM] Rahim: text
LINE_RE = re.compile(
    r"^\[?(?P<date>\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}),?\s+"
    r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)[\s\u202f]*(?P<ampm>[AaPp]\.?\s?[Mm]\.?)?\]?"
    r"\s*(?:-\s*)?(?P<sender>[^:]{1,60}):\s(?P<text>.*)$"
)

# A line that starts with a date but has no "Name: " part is a system notice
# ("Messages are end-to-end encrypted", "X changed their number"...). We skip those.
DATE_START_RE = re.compile(r"^\[?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4},?\s+\d{1,2}[:.]\d{2}")

SKIP_TEXTS = (
    "<media omitted>", "image omitted", "video omitted", "audio omitted", "sticker omitted",
    "gif omitted", "document omitted", "this message was deleted", "you deleted this message",
    "null", "<attached:",
)


def parse_timestamp(date: str, time_: str, ampm: str | None, month_first: bool) -> int | None:
    a, b, year = re.split(r"[/.\-]", date)
    day, month = (int(b), int(a)) if month_first else (int(a), int(b))
    year = int(year) + (2000 if len(year) == 2 else 0)
    t = [int(x) for x in re.split(r"[:.]", time_)]
    hour, minute, second = t[0], t[1], (t[2] if len(t) > 2 else 0)
    if ampm:
        pm = ampm.lower().startswith("p")
        hour = (hour % 12) + (12 if pm else 0)
    try:
        return int(datetime(year, month, day, hour, minute, second).timestamp())
    except ValueError:
        return None


def parse_file(path: Path, month_first: bool) -> list[dict]:
    """Returns messages: {timestamp, sender, text}. Multi-line messages are joined."""
    messages = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.translate(INVISIBLE).strip()
        m = LINE_RE.match(line)
        if m:
            ts = parse_timestamp(m["date"], m["time"], m["ampm"], month_first)
            if ts is None and messages:
                ts = messages[-1]["timestamp"]
            if ts is None:
                continue
            messages.append({"timestamp": ts, "sender": m["sender"].strip(), "text": m["text"].strip()})
        elif DATE_START_RE.match(line):
            continue  # system notice, not a real message
        elif messages and line:
            messages[-1]["text"] += "\n" + line  # continuation of a multi-line message
    # Drop media placeholders, deleted messages and edit tags.
    cleaned = []
    for msg in messages:
        text = msg["text"].replace("<This message was edited>", "").strip()
        if not text or any(text.lower().startswith(s) for s in SKIP_TEXTS):
            continue
        cleaned.append({**msg, "text": text})
    return cleaned


def main():
    ap = argparse.ArgumentParser(description="Import an exported WhatsApp chat into agent.db")
    ap.add_argument("file", type=Path)
    ap.add_argument("--me", help="your name exactly as in the file")
    ap.add_argument("--number", help="the contact's phone number, e.g. 8801712345678")
    ap.add_argument("--mdy", action="store_true", help="dates are month/day (US phones)")
    ap.add_argument("--list-senders", action="store_true", help="only show who is in the file")
    args = ap.parse_args()

    msgs = parse_file(args.file, month_first=args.mdy)
    senders = Counter(m["sender"] for m in msgs)

    if args.list_senders or not (args.me and args.number):
        print(f"{len(msgs)} messages found. Senders:")
        for name, count in senders.most_common():
            print(f"  {count:6d}  {name}")
        if not args.list_senders:
            print("\nNow run again with --me \"<your name>\" --number <their number>")
        return

    if args.me not in senders:
        raise SystemExit(f'"{args.me}" is not a sender in this file. Use --list-senders to check the spelling.')
    others = [s for s in senders if s != args.me]
    if len(others) != 1:
        raise SystemExit(f"Expected a one-to-one chat but found {len(others)} other senders. Group chats are not supported.")
    their_name = others[0]

    db.init_db()
    number = args.number.lstrip("+").replace(" ", "")
    # Reuse the contact if the bridge already saw them (keeps their real WhatsApp chat id).
    contact = db.get_contact(number) or db.upsert_contact(number, f"{number}@c.us", their_name)

    added = mine = 0
    seen = Counter()
    for m in msgs:
        mine_flag = m["sender"] == args.me
        key = f'{m["timestamp"]}|{m["sender"]}|{m["text"]}'
        seen[key] += 1  # identical messages in the same minute stay separate
        msg_id = "export:" + number + ":" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:20]
        is_new = db.save_message(
            msg_id, contact["chat_id"], number,
            "out" if mine_flag else "in",
            "export" if mine_flag else "contact",
            "chat", m["text"], m["timestamp"],
        )
        added += is_new
        mine += is_new and mine_flag

    print(f"Imported {added} new messages with {their_name} ({number}); {mine} of them are yours.")
    print(f"Skipped {len(msgs) - added} already imported.")


if __name__ == "__main__":
    main()
