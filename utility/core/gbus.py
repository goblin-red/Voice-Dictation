#!/usr/bin/env python3
"""Manage terminal conversations through the GOBL(in) Voice Gateway.

Open and close conversations in Voice. Replies are delivered automatically.

  gbus list             terminals and names
  gbus channels [ID]    conversations and events
  gbus routes           routes for this terminal
  gbus whoami           current session
  gbus read ID          full saved message
"""
import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "http://127.0.0.1:8792/api/orch/"
SPOOL = "/tmp/goblin-orch"      # запасной канал для агентов в песочнице без сети


def my_tty(pid=None):
    """tty вкладки: у shell-инструмента агента его нет, поднимаемся по родителям до процесса с tty.
    В песочнице (Codex read-only) ps запрещён — тогда None, и tty по нашему pid найдёт сам Voice."""
    pid = pid or os.getpid()
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=3).stdout.split()
        except Exception:
            return None
        if len(out) < 2:
            return None
        ppid, tty = out[0], out[1]
        if tty not in ("??", "-"):
            return "/dev/" + tty
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, PermissionError)) or "not permitted" in str(e.reason):
            return spool(path, body)     # песочница агента (Codex) не пускает в сеть — через файл
        return {"ok": False, "error": f"Voice did not respond: {e.reason}"}
    except (ConnectionRefusedError, PermissionError):
        return spool(path, body)
    except OSError as e:                 # таймаут: запрос мог дойти — повтором не дублируем
        return {"ok": False, "error": f"Voice timed out ({e}). Check gbus list"}


def spool(path, body):
    """Запасной канал: запрос файлом в SPOOL, Voice подхватывает и кладёт ответ рядом."""
    rid = f"{os.getpid()}-{int(time.time() * 1000)}"
    try:
        os.makedirs(SPOOL, exist_ok=True)
        tmp = os.path.join(SPOOL, rid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"path": path, "body": body}, f, ensure_ascii=False)
        os.rename(tmp, os.path.join(SPOOL, rid + ".req.json"))
    except OSError as e:
        return {"ok": False, "error": f"Could not reach GOBL(in) Voice over the network or through {SPOOL} ({e})"}
    res = os.path.join(SPOOL, rid + ".res.json")
    for _ in range(600):
        if os.path.exists(res):
            try:
                with open(res, encoding="utf-8") as f:
                    out = json.load(f)
                try:
                    os.remove(res)
                except OSError:
                    pass
                return out
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    try:
        os.remove(os.path.join(SPOOL, rid + ".req.json"))      # не забрали — значит Voice не запущен
        return {"ok": False, "error": "GOBL(in) Voice is not running"}
    except OSError:
        return {"ok": False, "error": "GOBL(in) Voice timed out. Check gbus list"}


AGENT_WORDS = {"claude", "codex", "grok", "клод", "клауд", "кодекс", "грок", "agy", "opencode", "pi", "аги", "опенкод", "пи"}


def me():
    return {"tty": my_tty(), "pid": os.getpid(), "rid": f"{os.getpid()}-{time.time():.6f}",
            "session": os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "",
            "term_session": os.environ.get("TERM_SESSION_ID") or ""}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "leader-ready":
        if len(rest) != 1:
            print("Usage: gbus leader-ready ASSIGNMENT_CODE")
            return 2
        res = call("leader-ready", {**me(), "nonce": rest[0]})
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if cmd == "read":
        if len(rest) != 1:
            print("Usage: gbus read ID")
            return 2
        res = call("read", {**me(), "id": rest[0]})
        if not res.get("ok"):
            print(res.get("error", "Message not found"))
            return 1
        print(json.dumps(res["envelope"], ensure_ascii=False, indent=2))
        return 0
    if cmd in ("whoami", "routes"):
        res = call("whoami", me())
        if not res.get("ok"):
            print(res.get("error", "Could not identify the terminal"))
            return 1
        if cmd == "routes":
            for route in res.get("routes", []):
                print(f"{route['channel']} · {route['peer']} {route['peer_tty']} · {route['topic']} · {route['status']}")
            return 0
        print(f"{res['nick']} · {res['name']} · {res['tty']} · ID: {res.get('id', '')}")
        return 0
    if cmd == "list":
        res = call("state")
        if not res.get("ok", True) and "error" in res:
            print("Error:", res["error"])
            return 1
        print("Gateway:", "on" if res.get("enabled") else "off (Voice → Gateway)")
        identity = call("whoami", me())
        mine = identity.get("tty") if identity.get("ok") else None
        for s in res.get("sessions", []):
            print(f"  {s.get('nick', ''):<6} {s['name']:<10} {s['tty']:<13} {s.get('state') or '—':<6} {s.get('cwd', '')}"
                  + f"  ID: {s.get('id', '')} · № {s.get('terminal_index') or '—'} · {s.get('description') or 'no description'}"
                  + ("   ← you" if s["tty"] == mine else ""))
        return 0
    if cmd == "channels":
        if len(rest) > 1:
            print("Usage: gbus channels [ID]")
            return 2
        res = call("state")
        if res.get("error"):
            print("Error:", res["error"])
            return 1
        shown = 0
        for th in res.get("threads", []):
            if not rest or th["id"] == rest[0]:
                shown += 1
                if th.get("stop_reason"):
                    print("Closed because:", th["stop_reason"])
                print(f"{th['id']} · {th.get('topic', '')} · {th['a']} ↔ {th['b']} · {th.get('mode', '')} · {th.get('phase', th['status'])} · #{th['hop']}")
        if rest and not shown:
            print(f"Channel not found: {rest[0]} (gbus channels)")
            return 1
        if rest:
            for event in res.get("feed", []):
                if event.get("thread") == rest[0]:
                    print(json.dumps(event, ensure_ascii=False))
        return 0
    if cmd == "send":
        mode, topic, channel, turns = "channel", None, None, None
        while rest and rest[0].startswith("--"):
            flag, rest = rest[0], rest[1:]
            if flag == "--dialog":
                mode = "dialog"
            elif flag == "--turns" and rest:
                try:
                    turns = int(rest.pop(0))
                except ValueError:
                    print("Turn count must be an integer")
                    return 2
            elif flag == "--channel" and rest:
                channel, rest = rest[0], rest[1:]
            elif flag == "--topic" and rest:
                topic, rest = rest[0], rest[1:]
                if not topic.strip() or len(topic) > 160:
                    print("Topic must have between 1 and 160 characters")
                    return 2
            else:
                print("Unknown option or missing value:", flag)
                return 2
        if not rest:
            print('Usage: gbus send <recipient> "text" (recipient: name, codex 2, or @ for most recent)')
            return 2
        if channel:
            rest = ["@", *rest]
        target = rest[0]
        # Номер после типа агента или имени; для текста, начинающегося с цифры, передавайте его одним аргументом.
        # Для «@» и --channel номер не нужен: цифра в начале текста («@ 5 минут») остаётся текстом.
        if target != "@" and ":/dev/" not in target and not target.startswith(("ttys", "/dev/ttys")) and len(rest) > 2 and rest[1].isdigit():
            target, rest = f"{target} {rest[1]}", rest[2:]
        else:
            rest = rest[1:]
        text = " ".join(rest).strip()
        if text == "-":
            text = sys.stdin.read().strip()
        if not text:
            print("Message is empty")
            return 2
        res = call("send", {**me(), "channel_protocol": 1, "to": target, "text": text, "mode": mode, **({"turns": turns} if turns is not None else {}), **({"topic": topic} if topic else {}), **({"channel": channel} if channel else {})})
    elif cmd == "reply":
        if len(rest) < 2:
            print('Usage: gbus reply <message ID> "reply"')
            return 2
        text = " ".join(rest[1:])
        if text == "-":
            text = sys.stdin.read()
        res = call("reply", {**me(), "id": rest[0], "text": text})
    elif cmd == "bind":
        if len(rest) != 1:
            print("Usage: gbus bind <your name from gbus list>")
            return 2
        res = call("bind", {**me(), "to": rest[0]})
    elif cmd == "end":
        if len(rest) > 1:
            print("Usage: gbus end [channel]")
            return 2
        res = call("end", {**me(), **({"thread": rest[0]} if rest else {})})
    else:
        print("Unknown command:", cmd)
        return 2
    print(res.get("message") or res.get("error") or json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
