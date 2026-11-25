"""Exact voice routing metadata; never inspect answer text or terminal history."""
import json
import os
from pathlib import Path
import re

BINDINGS = Path(__file__).resolve().parent.parent / "data" / "state" / "voice-terminal-bindings.json"
SESSION_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.jsonl)?$", re.I)


def load_bindings(path=BINDINGS):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def event_tty(event, sessions, history=None, bindings=None):
    """Resolve source by exact log or explicitly captured session ID + live PID.

    history is accepted for compatibility but is NEVER called. A closed/reused
    tty cannot inherit a binding: both tty and agent PID must still match.
    """
    tty, pid = event.get("tty"), event.get("pid")
    if tty and pid:
        exact_source = [s for s in sessions if s.get("tty") == tty and s.get("pid") == pid and s.get("agent") == event.get("agent")]
        if len(exact_source) == 1:
            return tty
    path = event.get("path")
    if not path:
        return None
    agent = event.get("agent")
    live = [s for s in sessions if s.get("agent") == agent]
    exact = {s["tty"] for s in live if s.get("log") and
             os.path.realpath(s["log"]) == os.path.realpath(path)}
    match = SESSION_ID.search(Path(path).name)
    if match:
        sid = match.group(1).lower()
        records = load_bindings() if bindings is None else bindings
        for record in records:
            if not isinstance(record, dict) or record.get("session_id", "").lower() != sid or record.get("agent") != agent:
                continue
            for s in live:
                if s.get("tty") == record.get("tty") and s.get("pid") == record.get("pid"):
                    exact.add(s["tty"])
    return exact.pop() if len(exact) == 1 else None
