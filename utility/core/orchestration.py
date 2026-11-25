"""Диалоги Voice. Владелец выбирает пары, агенты адресуют реплики меткой. См. docs/gbus.md."""
import datetime
import glob
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from direct import Direct, _as_string, _osascript
import gateway_channel_guard as guard

TERMINAL = "com.apple.Terminal"
ITERM = "com.googlecode.iterm2"
AGENTS = ("claude", "codex", "grok", "agy", "opencode", "pi")
EXPLICIT_REPLY = set(AGENTS)  # каждый ответ явно привязан к сообщению, даже при нескольких письмах за ход
ALIASES = {"claude": "claude", "клод": "claude", "клауд": "claude", "клода": "claude",
           "codex": "codex", "кодекс": "codex", "кодексу": "codex",
           "grok": "grok", "грок": "grok", "гроку": "grok",
           "agy": "agy", "аги": "agy", "opencode": "opencode", "опенкод": "opencode",
           "pi": "pi", "пи": "pi"}
# мнемонические имена вкладок: (как пишем, латиницей — тоже понимаем)
NAMES_BY_AGENT = {
    "codex": [('Cameron', 'cameron'), ('Casey', 'casey'), ('Charlie', 'charlie'), ('Cody', 'cody'), ('Cory', 'cory')],
    "opencode": [('Oakley', 'oakley'), ('Oliver', 'oliver'), ('Oscar', 'oscar'), ('Owen', 'owen'), ('Otis', 'otis')],
    "agy": [('Alex', 'alex'), ('Andy', 'andy'), ('Avery', 'avery'), ('Ashton', 'ashton'), ('Arthur', 'arthur')],
    "grok": [('Gabe', 'gabe'), ('Gale', 'gale'), ('George', 'george'), ('Glen', 'glen'), ('Grayson', 'grayson')],
    "claude": [('Blake', 'blake'), ('Carter', 'carter'), ('Claire', 'claire'), ('Clay', 'clay'), ('Cliff', 'cliff')],
    "pi": [('Pat', 'pat'), ('Parker', 'parker'), ('Perry', 'perry'), ('Peter', 'peter'), ('Piper', 'piper')],
}
NAMES = [pair for pool in NAMES_BY_AGENT.values() for pair in pool]


def _nick_aliases(nick):
    base, _, suffix = nick.partition(" ")
    latin = dict(NAMES).get(base, base)
    return {" ".join(value.lower().replace("ё", "е").replace("-", " ").split()) for value in
            (nick, latin + (" " + suffix if suffix else ""))}

TRANSFER = re.compile(r"(трансфер|transfer)", re.I)
GBUS_LINE = re.compile(r"\s*(\[gbus |\{\"type\": ?\"agent_message\")")   # письмо шлюза, а не запрос пользователя
DIALOG = re.compile(r"(трансфер|transfer)\W+(диалог|dialog)", re.I)   # «трансфер диалог боря …» — агенты говорят сами
HOPS_CAP = 200          # лимит ходов выключен — всё равно не больше стольких писем в диалоге
SENDER_MUTE = 40.0       # столько секунд не озвучиваем вкладку, которая только что переслала письмо
# конец диалога — [DONE] в самом конце ответа и отдельно (после точки или с новой строки), не «поставь [DONE]»
DONE_RE = re.compile(r"(^|[.!?…»)\n]\s*)[*_`]*\[DONE\][*_`]*[\s.!»\"')]*$")

ENTER_GAP = 0.5          # пауза между текстом и Enter — иначе агент примет Enter за часть вставки
ENTER_CHECK = 3.0        # письмо не появилось в журнале за столько секунд — Enter ещё раз (только если агент свободен)
ENTER_RETRIES = 2
FALLBACK_AFTER = 6.0     # метки нет в известном журнале — ищем и в остальных свежих (агента перезапустили)
GROK_QUIET = 4.0         # Grok: ответ готов, когда журнал молчит столько секунд
SCAN_EVERY = 3.0         # список вкладок обновляем не чаще
LONG_TEXT = 3000         # длиннее — кладём в файл, в письме путь
MUTE_GRACE = 20.0        # столько секунд после ответа его не озвучиваем (AnswerWatcher может увидеть позже нас)
RID_TTL = 3600.0         # так долго помним id запроса gbus (защита от повтора); старше — забываем, чтобы память не росла

CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
SPOOL = "/tmp/goblin-orch"   # gbus из песочницы без сети кладёт сюда запросы файлами

TABS_SCRIPT = '''
tell application "Terminal"
  set out to ""
  repeat with w in windows
    repeat with i from 1 to count of tabs of w
      set out to out & (tty of tab i of w) & (ASCII character 9) & (id of w) & (ASCII character 9) & i & linefeed
    end repeat
  end repeat
  return out
end tell'''

ITERM_TABS_SCRIPT = '''
tell application "iTerm2"
  set out to ""
  repeat with wi from 1 to count of windows
    set w to window wi
    repeat with ti from 1 to count of tabs of w
      repeat with sess in sessions of tab ti of w
        set out to out & (tty of sess) & (ASCII character 9) & wi & (ASCII character 9) & ti & (ASCII character 9) & (id of sess) & linefeed
      end repeat
    end repeat
  end repeat
  return out
end tell'''

ITERM_WRITE_SCRIPT = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with sess in sessions of t
        if tty of sess is %s then
          tell sess to write text %s newline NO
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
  return "none"
end tell'''

DO_SCRIPT = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        do script %s in t
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''


HISTORY_SCRIPT = '''
tell application "Terminal"
  repeat with w in windows
    repeat with i from 1 to count of tabs of w
      if tty of tab i of w is %s then return history of tab i of w
    end repeat
  end repeat
  return ""
end tell'''


def _read_jsonl(path, offset=0):
    """Строки журнала с offset: [(конец строки, dict)]; недописанную последнюю строку пропускаем."""
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return out
    pos = offset
    for raw in data.split(b"\n")[:-1]:
        pos += len(raw) + 1
        try:
            out.append((pos, json.loads(raw)))
        except ValueError:
            pass
    return out


def _claude_user_text(d):
    """Текст запроса пользователя в журнале Claude (None — это не запрос: результат инструмента, служебное)."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list) and not any(b.get("type") == "tool_result" for b in c if isinstance(b, dict)):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return None


def _codex_user_text(d):
    p = d.get("payload") or {}
    if d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
        text = " ".join(b.get("text", "") for b in p.get("content") or [] if isinstance(b, dict))
        if not text.startswith(("# AGENTS.md", "<environment_context>", "<user_instructions>")):
            return text
    return None


def _grok_user_text(d):
    if d.get("type") == "user":
        c = d.get("content")
        return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return None


USER_TEXT = {"claude": _claude_user_text, "codex": _codex_user_text, "grok": _grok_user_text}


class Orchestra:
    def __init__(self, cfg, log, journal):
        self.cfg = cfg
        self.log = log
        self.journal = Path(journal)            # logs/orchestration.jsonl — весь обмен, по записи на письмо
        self.lock = threading.RLock()
        self._scan_lock = threading.Lock()      # одно сканирование вкладок за раз
        self.wake = threading.Event()
        self.paused = False
        self.threads = {}                       # id треда -> тред
        self.feed = []                          # последние записи для окна
        self.names_file = self.journal.parent / "orch_names.json"   # имена переживают перезапуск Voice
        self.custom_names = set()
        self.descriptions = {}
        self.leader_session = None
        self.leader_ready = False
        self.leader_nonce = None
        self.on_leader_answer = lambda side, text: None
        self.name_history = {}
        self.names = self._load_names()         # (tty, pid агента) -> мнемоническое имя
        self._sessions, self._scanned = [], 0.0
        self._muted = {}                        # журнал -> до какого времени не озвучивать
        self.tab_logs = {}                      # (tty, pid) -> журнал сессии (по метке письма или по id сессии)
        self.sender_bindings, self.binding_birth = {}, {}
        self._load_sender_bindings()
        self.used_gates = set()                 # (журнал, позиция, tty, pid): одна команда может обращаться к разным получателям
        self.rids = {}                          # id запроса gbus -> ответ (повтор запроса не создаёт второй тред)
        self.finished_channels = {}
        self.ui_starts = []                     # «Старт» из окна: ждут, пока вкладка A освободится
        self.write_locks = {}                   # только атомарность вставки; ответа агента не ждём
        self.deliveries = set()                 # каналы, которые сейчас печатаются
        self.last_to = {}                       # tty отправителя -> (tty, pid) последнего получателя
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="orchestra", daemon=True)
        self._spooler = threading.Thread(target=self._run_spool, name="orchestra-spool", daemon=True)

    # ---------- жизненный цикл

    def _save_channel(self, th):
        """Caller holds self.lock. Write-ahead snapshot includes queued delivery IDs."""
        if th.get("mode") != "channel":
            return
        directory = self.journal.parent / "orch" / "channels"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (th["id"] + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(th, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _remember_finished(self, th, ended):
        self.finished_channels[th["id"]] = {
            "id": th["id"], "status": "stopped", "ended": ended, "topic": th["topic"],
            "a": th["a"]["nick"], "b": th["b"]["nick"], "a_tty": th["a"]["tty"],
            "b_tty": th["b"]["tty"], "mode": th["mode"], "phase": "stopped", "hop": th["hop"]}
        if len(self.finished_channels) > 10:
            oldest = min(self.finished_channels, key=lambda key: self.finished_channels[key]["ended"])
            del self.finished_channels[oldest]

    def conversation(self, tid, after=0, limit=10):
        if type(limit) is not int or limit not in (10, 50, 100):
            return {"ok": False, "error": "Choose 10, 50 or 100 messages"}
        with self.lock:
            th = self.threads.get(tid) or self.finished_channels.get(tid)
            if not th or not re.fullmatch(r"t[\w-]+", tid):
                return {"ok": False, "error": "Conversation not found"}
            last = th["hop"]
        messages = []
        for seq in range(last, max(0, after), -1):
            path = self.journal.parent / "orch" / f"{tid}h{seq}.json"
            try:
                msg = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"ok": False, "error": "Could not read conversation history"}
            if not msg.get("notice"):
                messages.append({k: msg.get(k) for k in ("seq", "from_name", "from_terminal", "body", "at", "author_role")})
                if len(messages) == limit:
                    break
        messages.reverse()
        return {"ok": True, "messages": messages, "last": last, "limit": limit}

    def _restore_channels(self):
        with self.lock:
            for path in (self.journal.parent / "orch" / "channels").glob("*.json"):
                try:
                    th = json.loads(path.read_text(encoding="utf-8"))
                    if th["status"] in ("done", "stopped"):
                        self._remember_finished(th, th.get("ended",path.stat().st_mtime))
                    if th["status"] in ("active", "suspended") and th["mode"] == "channel":
                        if th.pop("in_flight", None):
                            th.update(status="suspended", phase="uncertain",
                                      stop_reason="Voice restarted during input. Check delivery")
                        if not th.get("addressed") and th["status"] == "active":
                            th.update(status="suspended", phase="delivery_error", stop_reason="This conversation uses an older protocol. Choose a pair in Voice to start again.")
                        self.threads[th["id"]] = th
                except (OSError, ValueError, KeyError) as exc:
                    self.log.warning("GBus: restoring %s: %s", path, exc)

    def start(self):
        self._restore_channels()
        self._note("restart", "Gateway started and restored persistent channels")
        self._worker.start()
        self._spooler.start()

    def stop(self):
        self._stop.set()
        self.wake.set()

    def enabled(self):
        return bool(self.cfg.get("orch_enabled"))

    def hops_limited(self):
        return bool(self.cfg.get("orch_hops_limit", True))

    def max_hops(self):
        """Лимит ходов; выключен — только страховка HOPS_CAP от бесконечной переписки."""
        if not self.hops_limited():
            return HOPS_CAP
        return max(2, int(self.cfg.get("orch_max_hops") or 5000))

    def _hop_label(self, hop):
        return f"turn {hop}/{self.max_hops()}" if self.hops_limited() else f"turn {hop}"

    def reply_timeout(self):
        return float(self.cfg.get("orch_reply_timeout", 1800) or 1800)

    # ---------- вкладки Terminal с агентами

    def speech_sessions(self):
        """Audio reads the last inventory; OS scanning happens off its thread."""
        with self.lock:
            snapshot = list(self._sessions)
            refresh = (time.time() - self._scanned >= SCAN_EVERY
                       and not getattr(self, "_speech_scan_pending", False))
            if refresh:
                self._speech_scan_pending = True
        if refresh:
            def update():
                try:
                    self.sessions(fresh=True)
                except Exception:
                    self.log.exception("Speech: refreshing terminal list")
                finally:
                    with self.lock:
                        self._speech_scan_pending = False
            threading.Thread(target=update, daemon=True, name="speech-terminal-scan").start()
        return snapshot

    def sessions(self, fresh=False):
        with self.lock:
            if not fresh and time.time() - self._scanned < SCAN_EVERY:
                return list(self._sessions)
        if not self._scan_lock.acquire(blocking=False):      # уже сканирует другой поток — берём прошлый список
            with self.lock:
                return list(self._sessions)
        try:
            found = self._scan()
        finally:
            self._scan_lock.release()
        with self.lock:
            if found is None:                                # сбой osascript / ps: список и имена не трогаем
                return list(self._sessions)
            alive = {(s["tty"], s["pid"]) for s in found}
            before = dict(self.names)
            # имя снимаем, только когда процесса агента больше нет — сбой сканирования имена не стирает
            self.names = {k: v for k, v in self.names.items() if k in alive or _pid_alive(k[1])}
            self.custom_names.intersection_update(self.names)
            self.descriptions = {k: v for k, v in self.descriptions.items() if k in self.names}
            self.name_history = {k: v for k, v in self.name_history.items() if k in self.names}
            self.tab_logs = {k: v for k, v in self.tab_logs.items() if k in alive or _pid_alive(k[1])}
            for s in found:
                key = (s["tty"], s["pid"])
                pool = NAMES_BY_AGENT[s["agent"]]
                current = self.names.get(key, "")
                # Старые общие имена меняем один раз; правильные сохраняем после перезапуска.
                base, _, suffix = current.partition(" ")
                if key not in self.custom_names and (base not in dict(pool) or (suffix and (not suffix.isdigit() or int(suffix) < 2))):
                    used = set().union(*(_nick_aliases(n) for k, name in self.names.items()
                                           for n in [name, *self.name_history.get(k, [])]))
                    free = [n for n, _ in pool if _nick_aliases(n).isdisjoint(used)]
                    if free:
                        self.names[key] = secrets.choice(free)
                    else:
                        number = 2
                        while not free:
                            free = [f"{n} {number}" for n, _ in pool if _nick_aliases(f"{n} {number}").isdisjoint(used)]
                            number += 1
                        self.names[key] = secrets.choice(free)
                s["description"] = self.descriptions.get(key, "")
                s["nick"] = self.names[key]
                s["id"] = s["tty"].removeprefix("/dev/")
                s["session_id"] = self._session_id(s)
            if self.names != before:
                self._save_names()
            self._sessions, self._scanned = found, time.time()
            self._prune_sender_bindings(found)
        self._close_dead_channels(found)
        return list(found)

    @staticmethod
    def _side_alive(side, alive):
        pid = side.get("pid")
        if (side.get("tty"), pid) not in alive and not _pid_alive(pid):
            return False
        return not side.get("birth") or _process_start(pid) == side["birth"]

    def _close_dead_channels(self, sessions):
        if sessions is None:
            return
        alive = {(s["tty"], s["pid"]) for s in sessions}
        with self.lock:
            candidates = [(th["id"], th["a"], th["b"]) for th in self.threads.values()
                          if th.get("mode") == "channel" and th["status"] in ("active", "suspended")]
        for tid, side_a, side_b in candidates:
            for side in (side_a, side_b):
                if not self._side_alive(side, alive):
                    self.stop_thread(tid, why=f"{side['nick']}: terminal closed")
                    break

    def _load_names(self):
        try:
            data = json.loads(self.names_file.read_text(encoding="utf-8"))
            names = {}
            for raw, value in data.items():
                tty, pid = raw.split("|")
                key = (tty, int(pid))
                if isinstance(value, str):
                    names[key] = value
                elif isinstance(value, dict) and isinstance(value.get("name"), str):
                    names[key] = value["name"]
                    self.descriptions[key] = str(value.get("description", ""))
                    self.custom_names.add(key)
                    self.name_history[key] = [n for n in value.get("aliases", []) if isinstance(n, str)]
            return names
        except (OSError, ValueError, IndexError, AttributeError, TypeError):
            return {}

    def _save_names(self, strict=False):
        try:
            data = {f"{t}|{p}": ({"name": n, "aliases": self.name_history.get((t, p), []), "description": self.descriptions.get((t, p), "")}
                    if (t, p) in self.custom_names or (t, p) in self.descriptions else n) for (t, p), n in self.names.items()}
            tmp = self.names_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.names_file)
        except OSError as e:
            self.log.warning("Gateway names: %s", e)
            if strict:
                raise

    def describe(self, data):
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 1000:
            return {"ok": False, "error": "Description is limited to 1,000 characters"}
        self.sessions(fresh=True)
        with self.lock:
            side = next((x for x in self._sessions if x["session_id"] == data.get("id")), None)
            if not side or not _pid_alive(side["pid"]):
                return {"ok": False, "error": "The terminal closed or the agent changed"}
            key = (side["tty"], side["pid"])
            old = self.descriptions.get(key, "")
            self.descriptions[key] = description
            try:
                self._save_names(strict=True)
            except OSError:
                self.descriptions[key] = old
                return {"ok": False, "error": "Description could not be saved"}
            side["description"] = description
            return {"ok": True}

    def rename(self, data):
        """Имя — подпись и псевдоним; адрес agent:PID:tty остаётся тем же."""
        name = " ".join(str(data.get("name") or "").split())
        if not re.fullmatch(r"[^\W\d_][\w -]{0,39}", name):
            return {"ok": False, "error": "Use 1–40 characters, starting with a letter. Letters, digits, spaces, hyphens and underscores are allowed"}
        if re.fullmatch(r"ttys\d+", name, re.I):
            return {"ok": False, "error": "Names starting with ttys are reserved for terminal addresses"}
        aliases = _nick_aliases(name)
        if any(re.fullmatch(r"(?:" + "|".join(map(re.escape, ALIASES)) + r")(?:\s*\d+)?", n) for n in aliases):
            return {"ok": False, "error": "The agent type is reserved. Choose a different name"}
        self.sessions(fresh=True)
        with self.lock:
            session = next((s for s in self._sessions if self._session_id(s) == data.get("id")), None)
            if not session or not _pid_alive(session["pid"]):
                return {"ok": False, "error": "The terminal closed or the agent restarted. Refresh the list"}
            key = (session["tty"], session["pid"])
            for other, current in self.names.items():
                if other != key and any(not aliases.isdisjoint(_nick_aliases(n))
                                        for n in [current, *self.name_history.get(other, [])]):
                    return {"ok": False, "error": "This name or a previous form already belongs to another terminal"}
            previous = self.names[key]
            history = list(self.name_history.get(key, []))
            custom = key in self.custom_names
            self.name_history[key] = list(dict.fromkeys([*history, previous]))
            self.custom_names.add(key)
            self.names[key] = name
            try:
                self._save_names(strict=True)
            except OSError:
                self.names[key] = previous
                self.name_history[key] = history
                if not custom:
                    self.custom_names.discard(key)
                return {"ok": False, "error": "Could not save the name"}
            session["nick"] = name
            # Участники каналов по-прежнему те же; обновляем только видимую подпись.
            for th in self.threads.values():
                for side in (th["a"], th["b"]):
                    if (side["tty"], side["pid"]) == key:
                        side["nick"] = name
            return {"ok": True, "id": self._session_id(session), "name": name}

    def _scan(self):
        """Terminal tabs and iTerm2 sessions, including split panes, by exact TTY."""
        ttys, terminal_numbers, surfaces = [], {}, {}
        for bundle, script in ((TERMINAL, TABS_SCRIPT), (ITERM, ITERM_TABS_SCRIPT)):
            if not Direct.is_running(bundle):
                continue
            try:
                rows = [x.strip().split("\t") for x in _osascript(script).splitlines()
                        if x.strip().startswith("/dev/")]
                ordered = sorted(rows, key=lambda row: (int(row[1]), int(row[2]), row[0]))
                for row in ordered:
                    tty = row[0]
                    if tty not in surfaces:
                        ttys.append(tty)
                        terminal_numbers[tty] = len(ttys)
                        surfaces[tty] = {"terminal_app": bundle,
                                         "terminal_key": row[3] if len(row) > 3 else tty}
            except Exception as e:
                self.log.debug("Gateway tabs %s: %s", bundle, e)
                return None  # A partial scan must not discard live identities.
        found = []
        for tty in ttys:
            agent_proc = _tty_agent(tty)
            if agent_proc is False:
                return None
            if agent_proc:
                found.append({"tty": tty, "pid": agent_proc[0], "agent": agent_proc[1],
                              "terminal_index": terminal_numbers.get(tty), **surfaces[tty]})
        for s in found:
            s.update(self._details(s))
        found.sort(key=lambda s: int(re.sub(r"\D", "", s["tty"]) or 0))   # номера по tty — не прыгают, когда окна меняют порядок
        counts = {}
        for s in found:
            counts[s["agent"]] = counts.get(s["agent"], 0) + 1
            s["n"] = counts[s["agent"]]
        for s in found:
            s["name"] = s["agent"] if counts[s["agent"]] == 1 else f"{s['agent']} {s['n']}"
        return found

    def _details(self, s):
        """Папка, состояние и журнал сессии (что удаётся узнать)."""
        info = {"cwd": "", "state": "", "log": None}
        if s["agent"] == "claude":
            try:
                d = json.loads((CLAUDE_SESSIONS / f"{s['pid']}.json").read_text())
                info["cwd"] = d.get("cwd", "")
                info["state"] = d.get("status", "")
                sid = d.get("sessionId")
                if sid:
                    hits = glob.glob(str(CLAUDE_PROJECTS / "*" / f"{sid}.jsonl"))
                    info["log"] = hits[0] if hits else None
            except (OSError, ValueError):
                pass
        if not info["cwd"]:
            try:
                out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", str(s["pid"]), "-Fn"], capture_output=True,
                                     text=True, timeout=3).stdout
                info["cwd"] = next((l[1:] for l in out.splitlines() if l.startswith("n/")), "")
            except Exception:
                pass
        with self.lock:
            known = self.tab_logs.get((s["tty"], s["pid"]))
        if not known:
            with self.lock:
                ids = [sid for sid, key in getattr(self, "sender_bindings", {}).items() if key == (s["tty"], s["pid"])]
            if len(ids) == 1:
                known = self._session_log({"agent": s["agent"]}, ids[0])
                if known:
                    self.tab_logs[(s["tty"], s["pid"])] = known
        # Voice discovery must not depend on an agent first calling gbus.
        # A standalone TUI owns its rollout; a shared daemon is never followed.
        if s["agent"] == "codex":
            discovered = _codex_open_log(s["pid"])
            if discovered:
                known = discovered
                with self.lock:
                    self.tab_logs[(s["tty"], s["pid"])] = discovered
        if s["agent"] == "grok":
            discovered = _grok_open_log(s["pid"])
            if discovered:
                known = discovered
                with self.lock:
                    self.tab_logs[(s["tty"], s["pid"])] = discovered
        info["log"] = info["log"] or known
        if s["agent"] == "codex":
            paths = [info["log"]] if info["log"] else []
            # Shared-server sessions are owned by the registered transport,
            # not by the daemon's cwd or the currently focused window.
            try:
                from terminal_registry import TerminalRegistry
                from terminal_sources import processes
                ids = TerminalRegistry().sessions_for_terminal(s["tty"], s["pid"], processes())
                registered_paths = []
                for sid in ids:
                    path = self._session_log(s, sid)
                    if path:
                        registered_paths.append(path)
                        if path not in paths:
                            paths.append(path)
                if registered_paths:
                    # Registry orders confirmed ownership by latest activity, not file mtime/cwd.
                    # Publish the same exact journal used for status to jroute details.
                    info["log"] = registered_paths[0]
                    with self.lock:
                        self.tab_logs[(s["tty"], s["pid"])] = info["log"]
            except (OSError, subprocess.SubprocessError):
                pass
            activities = [(path, _codex_activity(path)) for path in paths]
            states = [activity["state"] for _, activity in activities]
            info["work_turn_ids"] = [activity["work_id"] for _, activity in activities
                                     if activity["state"] == "busy" and activity["work_id"]]
            if states:
                info["state"] = "busy" if "busy" in states else "idle" if all(x == "idle" for x in states) else ""
                info["work_id"] = "|".join(path + ":" + activity["work_id"] for path, activity in activities
                                           if activity["state"] == "busy" and activity["work_id"])
        return info

    def resolve(self, target, exclude_tty=None):
        """«codex», «codex 2», «кодекс 2», «боря» → сессия. Голое имя агента — первая, кроме самого отправителя."""
        raw_target = str(target).strip()
        target = " ".join(str(target).lower().replace("ё", "е").replace("-", " ").split())
        sess = self.sessions(fresh=True)
        for s in sess:
            if raw_target == self._session_id(s) or raw_target in (s["tty"], s["tty"].removeprefix("/dev/")):
                return s
        with self.lock:
            for s in sess:
                key = (s["tty"], s["pid"])
                if any(target in _nick_aliases(n) for n in [s["nick"], *self.name_history.get(key, [])]):
                    return s
        m = re.fullmatch(r"(\S+?)\s*(\d+)?", target)
        if not m or ALIASES.get(m.group(1)) is None:
            return None
        agent, n = ALIASES[m.group(1)], m.group(2)
        cands = [s for s in sess if s["agent"] == agent]
        if n:
            return next((s for s in cands if s["n"] == int(n)), None)
        others = [s for s in cands if s["tty"] != exclude_tty]
        return (others or cands or [None])[0]

    # ---------- письма

    def send(self, data):
        """От gbus: личность, адресат, текст, тема и rid; политика трансфера — из настроек."""
        rid = str(data.get("rid") or "")
        # Проверка повтора и создание — одна транзакция, включая одновременный HTTP/spool.
        with self.lock:
            sender = self._sender(data)
            if not sender:
                return {"ok": False, "error": "The sender could not be identified. Run gbus bind <your name from gbus list>"}
            key = (sender["tty"], sender["pid"], rid)
            signature = json.dumps({k: data.get(k) for k in ("to", "text", "mode", "topic", "channel")}, sort_keys=True)
            if rid and key in self.rids:
                _, previous, out = self.rids[key]
                return out if previous == signature else {"ok": False, "error": "This request ID was already used for a different message"}
            out = self._send(data)
            if rid:
                now = time.time()
                self.rids = {k: v for k, v in self.rids.items() if now - v[0] < RID_TTL}   # повтор нужен только в первые минуты
                self.rids[key] = (now, signature, out)
            return out

    def _send(self, data):
        if not self.enabled():
            return {"ok": False, "error": "The gateway is off. Open Voice → Gateway to turn it on."}
        if not self._worker.is_alive():
            return {"ok": False, "error": "The Voice gateway is not running"}
        text = str(data.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Message is empty"}
        sender = self._sender(data)
        if not sender:
            return {"ok": False, "error": "The sender could not be identified. Run gbus bind <your name from gbus list> before sending"}
        if self._is_leader(sender):
            return {"ok": False, "error": "Use jroute to send the owner's message directly from the lead terminal"}
        to = str(data.get("to") or "").strip()
        channel = str(data.get("channel") or "")
        if channel:
            with self.lock:
                th = self.threads.get(channel)
                if not th or th["status"] != "active":
                    return {"ok": False, "error": "Channel not found or closed"}
                mine = next((k for k in ("a", "b") if (th[k]["tty"], th[k]["pid"]) == (sender["tty"], sender["pid"])), None)
                if mine is None:
                    return {"ok": False, "error": "The channel belongs to different sessions"}
                if th[mine].get("birth") and _process_start(sender["pid"]) != th[mine]["birth"]:
                    return {"ok": False, "error": "The sender process changed"}
                target = dict(th["b" if mine == "a" else "a"])
        elif to in ("", "@", "last", "последний"):
            target = self._last_target(sender)            # «трансфер <текст>» без имени — последнему, кому писали
        else:
            target = self.resolve(to, exclude_tty=sender["tty"])
        if not target:
            return {"ok": False, "error": f"Unknown recipient: {data.get('to')} (gbus list)"}
        if self._is_leader(sender) and target["tty"] in self.cfg.get("orch_leader_excluded_ttys", []):
            return {"ok": False, "error": "The owner excluded this terminal from lead assignments"}
        if self._is_leader(target):
            return {"ok": False, "error": "The recipient is the lead and cannot join GBus"}
        if target["tty"] == sender["tty"]:
            return {"ok": False, "error": "Choose a different recipient"}
        topic = data.get("topic")
        if topic is not None and (not isinstance(topic, str) or not topic.strip() or len(topic) > 160):
            return {"ok": False, "error": "Topic must have between 1 and 160 characters"}
        gate, mode = None, ("channel" if data.get("channel_protocol") == 1 else data.get("mode", "relay"))
        if mode not in ("channel", "dialog", "relay"):
            return {"ok": False, "error": "Unknown channel mode"}
        if self.cfg.get("orch_require_transfer", True) and not (channel and th.get("mode") == "channel"):
            gate, requested_mode, err = self._transfer_gate(sender, data.get("session"), target)
            if err:
                return {"ok": False, "error": err}
            if mode != "channel":
                mode = requested_mode
        with self.lock:
            self.last_to[sender["tty"]] = (target["tty"], target["pid"])
            channel = str(data.get("channel") or "")
            if channel:
                th = self.threads.get(channel)
                if not th or th["status"] != "active":
                    return {"ok": False, "error": "Channel not found or closed"}
                pair = {(x["tty"], x["pid"]) for x in (sender, target)}
                if pair != {(th[k]["tty"], th[k]["pid"]) for k in ("a", "b")}:
                    return {"ok": False, "error": "The channel belongs to different sessions"}
                if topic is not None and topic != th["topic"]:
                    return {"ok": False, "error": "Start a new channel to use a different topic"}
                if th.get("mode") == "channel":
                    self._guard_channel(th)
                    watch = th.get("watches", {}).get(mine, {})
                    turn = watch.get("current_turn")
                    if turn:
                        if guard.asks_approval(text):
                            watch["awaiting"] = True
                        tag = turn + ":" + guard.signature(text)
                        previous = watch.get("mirrored", {}).get(tag)
                        if previous:
                            return {"ok": True, "channel": channel, "message_id": previous,
                                    "message": "This message is already queued for fallback delivery"}
                        watch.setdefault("sent", {})[tag] = True
                self._enqueue(th, sender, target, text)
            else:
                limit = data.get("turns")
                if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 2 <= limit <= HOPS_CAP):
                    return {"ok": False, "error": "Turn count must be between 2 and " + str(HOPS_CAP)}
                try:
                    th = self._new_thread(sender, target, text, mode, topic=topic, turns=limit)
                except ValueError as error:
                    return {"ok": False, "error": str(error)}
            mid = th["last_message"]
        self._mute(sender.get("log"), SENDER_MUTE)       # реплику отправителя после пересылки вслух не читаем
        return {"ok": True, "channel": th["id"], "message_id": mid,
                "message": f"Accepted for immediate delivery to {target['nick']}, channel {th['id']}, message {mid}. "
                           "You can send more messages now. Check status with gbus channels."}

    def _last_target(self, sender):
        """Последний, кому писала эта вкладка; если не писала — единственный другой агент."""
        sess = self.sessions(fresh=True)
        with self.lock:
            key = self.last_to.get(sender["tty"])
        hit = next((s for s in sess if key and (s["tty"], s["pid"]) == key), None)
        if hit:
            return hit
        others = [s for s in sess if s["tty"] != sender["tty"]]
        return others[0] if len(others) == 1 else None

    def _busy_error(self, sender, target):
        """Занятость и другие каналы не запрещают новую отправку."""
        return None

    def _load_sender_bindings(self):
        try:
            rows = json.loads((self.journal.parent / "orch_senders.json").read_text())
            for row in rows:
                sid, tty, pid, birth = row["session"], row["tty"], int(row["pid"]), row["birth"]
                if birth and _process_start(pid) == birth:
                    self.sender_bindings[sid] = (tty, pid)
                    self.binding_birth[sid] = birth
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save_sender_bindings(self):
        if not getattr(self, "journal", None):
            return
        path = self.journal.parent / "orch_senders.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"session": sid, "tty": key[0], "pid": key[1], "birth": self.binding_birth[sid]}
                for sid, key in self.sender_bindings.items() if self.binding_birth.get(sid)]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)

    def _remember_sender(self, sid, tab):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", sid):
            return
        birth = _process_start(tab["pid"])
        if not birth:
            return
        with self.lock:
            key = (tab["tty"], tab["pid"])
            bindings = getattr(self, "sender_bindings", {})
            births = getattr(self, "binding_birth", {})
            if bindings.get(sid) == key and births.get(sid) == birth:
                return
            bindings[sid], births[sid] = key, birth
            self.sender_bindings, self.binding_birth = bindings, births
            self._save_sender_bindings()

    def _prune_sender_bindings(self, sessions):
        with self.lock:
            live = {(s["tty"], s["pid"]) for s in sessions}
            stale = [sid for sid, key in getattr(self, "sender_bindings", {}).items()
                     if key not in live or (getattr(self, "binding_birth", {}).get(sid)
                         and _process_start(key[1]) != self.binding_birth[sid])]
            for sid in stale:
                self.sender_bindings.pop(sid, None)
                getattr(self, "binding_birth", {}).pop(sid, None)
            if stale:
                self._save_sender_bindings()

    def bind(self, data):
        """Explicit session-to-terminal binding; never infer identity from conversation text."""
        # Native tools may have no vendor session ID; verified ancestry is sufficient.
        tab = self.resolve(str(data.get("to") or ""))
        if not tab:
            return {"ok": False, "error": "Tab not found. Run gbus list"}
        managed = _managed_codex_parent(_ancestor_pids(data.get("pid")))
        if not managed and tab["pid"] in _ancestor_pids(data.get("pid")):
            return {"ok": True, "message": f"Identified sender {tab['nick']} by process. No binding is needed"}
        sid = str(data.get("session") or "")
        if tab and tab["agent"] == "opencode":
            verified = self._opencode_sender(data, self.sessions(fresh=True))
            if verified and (verified["tty"], verified["pid"]) == (tab["tty"], tab["pid"]):
                return {"ok": True, "message": f"Identified OpenCode session {tab['nick']} ({tab['tty']}). No binding is needed"}
            return {"ok": False, "error": "Could not verify the OpenCode session in the selected tab"}
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", sid):
            return {"ok": False, "error": "A valid agent session ID is required"}
        tab = self.resolve(str(data.get("to") or ""))
        if not tab:
            return {"ok": False, "error": "Tab not found. Run gbus list"}
        path = self._session_log({"agent": tab["agent"]}, sid)
        if not path:
            return {"ok": False, "error": "Could not find a session log for this agent"}
        tty = _tty_of(data.get("pid"))
        if not managed and tty and tty != tab["tty"]:
            return {"ok": False, "error": "The selected tab does not match the sender terminal"}
        key = (tab["tty"], tab["pid"])
        with self.lock:
            bindings = getattr(self, "sender_bindings", {})
            if sid in bindings and bindings[sid] != key:
                return {"ok": False, "error": "The session is already bound to a different tab"}
            if any(k != sid and v == key for k, v in bindings.items()):
                return {"ok": False, "error": "The tab is already bound to a different session"}
            bindings[sid] = key
            self.sender_bindings = bindings
            self.tab_logs[key] = path
            self._remember_sender(sid, tab)
        return {"ok": True, "message": f"Session bound: {tab['nick']} ({tab['tty']})"}

    def _sender(self, data):
        """Use process ancestry or an explicit session binding; uncertainty blocks sending."""
        sess = self.sessions(fresh=True)
        self._prune_sender_bindings(sess)
        ancestors = _ancestor_pids(data.get("pid"))
        found = next((tab for pid in ancestors for tab in sess if tab["pid"] == pid), None)
        tty = _tty_of(data.get("pid"))
        found = found or next((s for s in sess if tty and s["tty"] == tty), None)
        if _managed_codex_parent(ancestors):
            found = None
        sid = str(data.get("session") or "")
        if sid.startswith("ses_"):
            verified = self._opencode_sender(data, sess, ancestors)
            if found and (not verified or (found["tty"], found["pid"]) != (verified["tty"], verified["pid"])):
                return None
            return verified
        with self.lock:
            key = getattr(self, "sender_bindings", {}).get(sid)
        bound = next((s for s in sess if (s["tty"], s["pid"]) == key), None)
        if key:
            if not bound or (found and (found["tty"], found["pid"]) != key):
                return None
            path = self._session_log({"agent": bound["agent"]}, sid)
            if not path:
                return None
            with self.lock:
                self.tab_logs[key] = path
            bound = dict(bound, log=path)
            return bound
        # A managed daemon can inherit the tty of an unrelated launcher TUI.
        if re.fullmatch(r"[0-9a-f-]{36}", sid):
            exact = self._codex_sender(data, sess)
            if exact:
                self._remember_sender(sid, exact)
                return exact
            if _managed_codex_parent(ancestors):
                return None
        if found:
            self._remember_sender(sid, found)
            return found
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", sid):
            return None
        codex = self._codex_sender(data, sess)
        if codex:
            return codex
        # Claude publishes its session ID in per-process metadata.
        path = self._session_log({"agent": "claude"}, sid)
        return self._tab_by_log("claude", path, sess) if path else None

    def whoami(self, data):
        tab = self._sender(data)
        if not tab:
            return {"ok": False, "error": "Could not match the process or session to a single tab"}
        with self.lock:
            routes = [{"channel": th["id"], "topic": th["topic"], "status": th["status"],
                       "peer": th[other]["nick"], "peer_tty": th[other]["tty"]}
                      for th in self.threads.values()
                      if th.get("mode") == "channel" and th["status"] in ("active", "suspended")
                      for mine, other in (("a", "b"), ("b", "a"))
                      if self._session_id(th[mine]) == self._session_id(tab)
                      and _process_start(tab["pid"]) == th[mine]["birth"]]
        return {"ok": True, "id": tab["tty"].removeprefix("/dev/"), "session_id": self._session_id(tab), "routes": routes, **{k: tab.get(k) for k in ("nick", "name", "agent", "tty", "pid")}}

    def _codex_sender(self, data, sessions):
        """Codex daemon: authoritative TUI diagnostic metadata, never user text or cwd."""
        sid = str(data.get("session") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", sid):
            return None
        if _process_identity(data.get("pid")).get("CODEX_THREAD_ID") != sid:
            return None
        db = Path.home() / ".codex" / "logs_2.sqlite"
        try:
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
                rows = conn.execute(
                    "SELECT process_uuid, ts FROM logs WHERE target IN ('codex_tui::app::recap', 'codex_tui::app::history_pagination') "
                    "AND instr(feedback_log_body, ?) > 0 ORDER BY id DESC LIMIT 100",
                    ("thread_id=" + sid + " ",)).fetchall()
        except (OSError, sqlite3.Error):
            return None
        matches = {}
        for tab in sessions:
            if tab["agent"] != "codex":
                continue
            try:
                env = dict(os.environ, LC_ALL="C")
                started = subprocess.run(["ps", "-p", str(tab["pid"]), "-o", "lstart="],
                                         capture_output=True, text=True, timeout=2, env=env).stdout.strip()
                start_ts = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
            if any(str(proc).startswith(f"pid:{tab['pid']}:") and ts >= start_ts for proc, ts in rows):
                matches[(tab["tty"], tab["pid"])] = tab
        return dict(next(iter(matches.values()))) if len(matches) == 1 else None

    def _opencode_sender(self, data, sessions, ancestors=None):
        """Сервер OpenCode отделён от TUI: сверяем реальное окружение инструмента и вкладки.
        ID из запроса сам по себе не подтверждает личность; cwd и текст экрана не используем.
        """
        sid = str(data.get("session") or "")
        if not re.fullmatch(r"ses_[a-zA-Z0-9_-]{4,124}", sid):
            return None
        identity = _process_identity(data.get("pid"))
        terminal = identity.get("TERM_SESSION_ID", "")
        if (identity.get("OPENCODE_SESSION_ID") != sid or identity.get("OPENCODE_TERMINAL") != "1"
                or not re.fullmatch(r"[a-zA-Z0-9:_-]{8,200}", terminal)):
            return None
        if data.get("term_session") and data["term_session"] != terminal:
            return None
        ancestors = _ancestor_pids(data.get("pid")) if ancestors is None else ancestors
        if not _opencode_parent(ancestors):
            return None
        matches = [tab for tab in sessions if tab["agent"] == "opencode"
                   and _process_identity(tab["pid"]).get("TERM_SESSION_ID") == terminal
                   and _foreground(tab["tty"], tab["pid"])]
        return dict(matches[0]) if len(matches) == 1 else None

    def _tab_by_log(self, agent, path, sess):
        """Only authoritative per-process metadata; cwd and screen text are not identity."""
        if agent != "claude" or not path:
            return None
        matches = []
        for tab in sess:
            if tab["agent"] != agent:
                continue
            try:
                meta = json.loads((CLAUDE_SESSIONS / f"{tab['pid']}.json").read_text())
            except (OSError, ValueError):
                continue
            if meta.get("sessionId") == Path(path).stem:
                matches.append(dict(tab, log=path))
        return matches[0] if len(matches) == 1 else None

    def _refresh_dialog_pair(self, th, sessions):
        """Rebind both selected terminals together before accepting another UI message."""
        if th.get("in_flight") or th["id"] in self.deliveries:
            return False
        replacements = {}
        for key in ("a", "b"):
            old = th[key]
            tab = sessions[old["tty"]]
            birth = _process_start(tab["pid"])
            if not birth:
                raise ValueError("The terminal is still starting")
            if (old["pid"], old.get("birth"), old.get("log")) == (tab["pid"], birth, tab.get("log")) and not th["watches"][key].get("invalid"):
                continue
            side = {field: tab.get(field) for field in old}
            side["birth"] = birth
            probe = {key: side}
            guard.attach(probe, key, tab.get("log"))
            if key not in probe.get("watches", {}):
                raise ValueError("The terminal log is not ready")
            watch = probe["watches"][key]
            watch.update(addressed=True, terminal_mode=th["watches"][key].get("terminal_mode"))
            replacements[key] = (side, watch)
        if not replacements:
            return True
        paths = [replacements[key][0]["log"] if key in replacements else th[key]["log"] for key in ("a","b")]
        if paths[0] == paths[1]:
            raise ValueError("Both terminals use the same log")
        for key, (side, watch) in replacements.items():
            th[key], th["watches"][key] = side, watch
        by_tty = {th[k]["tty"]: th[k] for k in ("a","b")}
        for item in th["queue"]:
            for field in ("to","frm"):
                item[field] = by_tty[item[field]["tty"]]
            item.pop("retry_at", None)
        self._save_channel(th)
        return True

    def start_from_ui(self, a, b, topic):
        """Register a pair only following the Voice start action."""
        if not self.enabled():
            return {"ok": False, "error": "The gateway is off"}
        def terminal(value):
            # Older open windows still submit agent:PID:/dev/ttysNNN.
            number = re.fullmatch(r"(?:(?:[^:]+:\d+:)?/dev/)?(?:ttys)?(\d+)", str(value))
            return self.resolve("ttys" + number[1].zfill(3) if number else value)
        sa, sb = terminal(a), terminal(b)
        if not sa or not sb or sa["tty"] == sb["tty"]:
            return {"ok": False, "error": "Select two different terminals"}
        if any(self._is_leader(x) for x in (sa, sb)):
            return {"ok": False, "error": "The lead cannot join conversations"}
        pair = {x["tty"] for x in (sa, sb)}
        with self.lock:
            for th in self.threads.values():
                if th.get("addressed") and th["status"] in ("active", "suspended") and pair == {th[k]["tty"] for k in ("a","b")}:
                    changed = any((old["pid"], old.get("log")) != (tab["pid"], tab.get("log"))
                                  for tab in (sa, sb) for old in (th["a"], th["b"]) if old["tty"] == tab["tty"])
                    recoverable = th["status"] == "suspended" and not th.get("in_flight") and (
                        (th.get("stop_reason") or "").endswith(("session closed or changed", "сессия закрыта или сменилась"))
                        or (th.get("stop_reason") or "").startswith(("The terminal log changed", "Журнал терминала сменился")))
                    if changed or recoverable:
                        if th["status"] == "suspended" and not recoverable:
                            return {"ok": False, "error": th.get("stop_reason") or "Check the delivery log"}
                        try:
                            if not self._refresh_dialog_pair(th, {s["tty"]: s for s in (sa, sb)}):
                                return {"ok": False, "error": "The previous delivery is still running"}
                        except ValueError as exc:
                            return {"ok": False, "error": str(exc)}
                        th.update(status="active", phase="queued" if th["queue"] else "open", stop_reason=None)
                        self.wake.set()
                    break
            else:
                try:
                    th = self._new_thread(sa, sb, None, "channel", topic=topic or "Conversation")
                except (ValueError, OSError) as exc:
                    return {"ok": False, "error": str(exc)}
            th["selected_at"] = time.time()
            self._save_channel(th)
            message = (topic or "").strip()
            if message:
                if th["status"] != "active":
                    return {"ok": False, "error": "The conversation is paused. The message was not sent"}
                source, recipient = (th["a"], th["b"]) if th["a"]["tty"] == sa["tty"] else (th["b"], th["a"])
                self._enqueue(th, source, source, message, notice=True, relay_to=recipient)
        return {"ok": True, "channel": th["id"], "status": th["status"]}

    def public_send(self, data):
        return {"ok": False, "error": "Open a conversation in Voice and start the message with [to:number]."}

    def end(self, data):
        sender = self._sender(data)
        if not sender:
            return {"ok": False, "error": "Sender not found"}
        tid = str(data.get("thread") or "").strip()
        with self.lock:
            ths = [th for th in self.threads.values() if th["status"] in ("active", "suspended")
                   and any((sender["tty"], sender["pid"]) == (th[k]["tty"], th[k]["pid"]) for k in ("a", "b"))
                   and (not tid or th["id"] == tid)]
        if any(th.get("mode") == "channel" for th in ths):
            return {"ok": False, "error": "The owner can stop the channel in Voice → Gateway → Stop"}
        for th in ths:
            self.stop_thread(th["id"], "closed by the agent")
        if not ths:
            return {"ok": False, "error": "You have no gateway conversation"}
        return {"ok": True, "message": f"Conversations closed: {len(ths)}"}

    def stop_thread(self, tid=None, why="stopped"):
        with self.lock:
            for th in self.threads.values():
                if th["status"] in ("active", "suspended") and (tid is None or th["id"] == tid):
                    th["status"] = "stopped"
                    th["ended"] = time.time()
                    if th.get("mode") == "channel":
                        self._remember_finished(th, th["ended"])
                    th["phase"] = "stopped"
                    th["queue"].clear()
                    th["pending"].clear()
                    self._save_channel(th)
                    self._note("info", f"{th['a']['nick']} ↔ {th['b']['nick']}: {why}", thread=th["id"])
            if tid is None:
                self.ui_starts.clear()
        self.wake.set()
        return {"ok": True}

    def set_paused(self, value):
        self.paused = bool(value)
        self._note("info", "paused" if self.paused else "resumed")
        self.wake.set()
        return {"ok": True}

    def _sender_from_receipt(self, data, msg):
        """Recover a detached Codex session from its exact delivered gateway receipt."""
        sid = str(data.get("session") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", sid):
            return None
        if _process_identity(data.get("pid")).get("CODEX_THREAD_ID") != sid:
            return None
        with self.lock:
            th = self.threads.get(msg["channel"], {})
            pending = th.get("pending", {}).get(msg["id"])
            if th.get("status") != "active":
                return None
            if th.get("mode") == "channel":
                side = next((th[k] for k in ("a", "b") if self._session_id(th[k]) == msg.get("to")), None)
                if not side or _process_start(side["pid"]) != side["birth"]:
                    return None
                since = th["started"]
            elif pending:
                side, since = pending["side"], pending["since"]
            else:
                return None
            if side.get("agent") != "codex" or self._session_id(side) != msg.get("to"):
                return None
            current = self._current(side)
            if not current or current["pid"] != side["pid"]:
                return None
            path = self._session_log({"agent": "codex"}, sid)
            if not path:
                return None
            # An exact, timestamped user-envelope in this verified session is the receipt.
            # Never infer from cwd, focus, ordinary answer text, or a quoted tool result.
            expected = _message_line(msg)
            try:
                with open(path, "rb") as handle:
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(max(0, size - 2 * 1024 * 1024))
                    if size > 2 * 1024 * 1024:
                        handle.readline()
                    records = handle.read().splitlines()
            except OSError:
                return None
            received = False
            for raw in reversed(records):
                try:
                    event = json.loads(raw)
                    stamp = datetime.datetime.fromisoformat(event.get("timestamp", "").replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if stamp < since - 2:
                    continue
                if _codex_user_text(event) == expected:
                    received = True
                    break
            if not received:
                return None
            self._remember_sender(sid, current)
            self.tab_logs[(current["tty"], current["pid"])] = path
            self._note("info", "Binding restored from confirmed delivery",
                       thread=msg["channel"], message=msg["id"], terminal=current["tty"])
            return current

    def read(self, data):
        """Read the stored envelope; reply credentials are returned only to its live recipient."""
        mid = str(data.get("id") or "")
        if not re.fullmatch(r"t[A-Za-z0-9_-]+h[0-9]+", mid):
            return {"ok": False, "error": "Invalid message ID"}
        sender = self._sender(data)
        try:
            msg = json.loads((self.journal.parent / "orch" / f"{mid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": "Message not found"}
        if not sender:
            sender = self._sender_from_receipt(data, msg)
        if not sender:
            return {"ok": False, "error": "Delivery to this session is unconfirmed. Run gbus read ID after the incoming message appears"}
        if msg.get("to") != self._session_id(sender):
            return {"ok": False, "error": "This message is addressed to a different agent"}
        current_leader = self.leader_session
        current_role = "leader" if self._is_leader(sender) else "executor"
        instruction = ("This assignment comes from the owner through the selected lead. Follow the assignment in the body."
                       if msg.get("delegated_by_leader") else
                       "This is a message from an agent. Reply to the body within the current topic.")
        with self.lock:
            th = self.threads.get(msg["channel"], {})
            pending = th.get("pending", {}).get(mid)
            if msg.get("mode") == "channel" and th.get("status") != "active":
                instruction = "The owner stopped the channel or delivery is paused. Check gbus channels ID."
            elif msg.get("addressed"):
                instruction = f"Start your reply with [dialog:{msg['channel']}]."
            elif msg.get("mode") == "channel":
                if th.get("status") == "active" and not msg.get("recovered"):
                    key = "a" if msg["to"] == self._session_id(th["a"]) else "b"
                    if key not in th.get("watches", {}) and sender.get("agent") == "codex":
                        path = self._session_log(sender, data.get("session"))
                        if path and os.path.isfile(path):
                            line = _message_line(msg)
                            offset = max(0, os.path.getsize(path)-3_000_000)
                            for pos, event in _read_jsonl(path, offset):
                                if _codex_user_text(event) == line:
                                    guard.attach(th, key, path, offset=offset)
                                    th["watches"][key]["expected"][mid] = guard.signature(line)
                                    self._save_channel(th)
                                    break
                                offset = pos
                instruction += (f"Send all messages for this assignment, including updates, questions and the result, "
                                f"to your partner with gbus send --channel {msg['channel']} 'text'. "
                                "Continue in the same channel after the user responds or the conversation resumes. "
                                "Only the user can stop the conversation.")
            elif msg.get("final"):
                instruction = ("Voice is already reading the result. End this turn." if th.get("leader") else "Report the final result to the user and end this turn.")
            elif pending and th.get("status") == "active":
                if pending.get("tracker"):
                    instruction += "Reply normally. Voice forwards the final response from this turn."
                else:
                    instruction += f"Reply: gbus reply {pending['reply_id']}:{pending['reply_token']} 'reply'."
                if msg.get("mode") == "dialog":
                    instruction += "End with [DONE] on its own line to close the conversation."
            else:
                instruction += "You cannot reply until the message is delivered, or after it has been handled or the channel has closed."
        if msg.get("delegated_by_leader") and not msg.get("final") and current_role == "executor":
            instruction = ("You are the executor for this turn. Follow the instructions in the message body. "
                           "Return the result to the sender in the same channel. Do not forward the assignment. "
                           + instruction)
        return {"ok": True, "envelope": {**msg, "instruction": instruction,
                "recipient_role_now": current_role,
                "current_leader": self._session_id(current_leader) if current_leader else None}}

    def reply(self, data):
        """Reply ticket ties detached tool processes to one pending message."""
        sender = self._sender(data)
        marker, _, token = str(data.get("id") or "").partition(":")
        text = str(data.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Reply is empty"}
        with self.lock:
            for th in self.threads.values():
                if th["status"] != "active":
                    continue
                pending = th.get("pending", {})
                w = pending.get(marker) or next((item for item in pending.values() if item.get("reply_id") == marker), None)
                if not w:
                    continue
                side = w["side"]
                if self._is_leader(side) or self._is_leader(w["other"]):
                    return {"ok": False, "error": "The participant is now the lead and cannot reply through GBus"}
                ticket_ok = bool(token and w.get("reply_token") and
                                 secrets.compare_digest(token, w["reply_token"]))
                if sender and (side["tty"], side["pid"]) != (sender["tty"], sender["pid"]):
                    return {"ok": False, "error": "This message is addressed to a different agent"}
                if w.get("reply_id") == marker and not ticket_ok:
                    return {"ok": False, "error": "Use the complete reply code ID:CODE"}
                if not sender and not ticket_ok:
                    return {"ok": False, "error": "Use the exact gbus reply ID:CODE command from the message"}
                if token and not ticket_ok:
                    return {"ok": False, "error": "Invalid reply code"}
                if not _pid_alive(side["pid"]):
                    return {"ok": False, "error": "The recipient session has closed. Its reply was rejected"}
                message_id = w["marker"]
                del th["pending"][message_id]
                self._on_reply(th, side, w["other"], text, reply_to=message_id)
                return {"ok": True, "message": "Reply accepted. You can process other messages independently."}
        return {"ok": False, "error": "The message was already handled or the conversation closed"}

    def ready_leader(self, data):
        return {"ok": False, "error": "The lead terminal changed. Run jroute ready in the selected lead terminal"}

    def _is_leader(self, side):
        leader = getattr(self, "leader_session", None)
        return bool(leader and side.get("tty") == leader["tty"] and side.get("pid") == leader["pid"])

    def _transfer_gate(self, sender, session, target):
        """Проверяем последнее прямое поручение владельца, пропуская ответы агентов.
        Полученный ответ не отменяет продолжение поручения. Возвращает (ключ команды, режим, ошибка):
        «трансфер диалог …» — автономный диалог агентов, просто «трансфер …» — пересылка (один вопрос — один ответ)."""
        path = self._session_log(sender, session)
        if not path:
            return None, None, "The sender session log is missing. Could not verify the transfer command"
        with self.lock:
            self.tab_logs[(sender["tty"], sender["pid"])] = path
        sender["log"] = path
        parse = USER_TEXT.get(sender["agent"])
        if parse is None:
            return None, None, "This agent has no supported session log and Require transfer command is enabled."
        last, at = None, None
        for pos, d in _read_jsonl(path, max(0, os.path.getsize(path) - 3_000_000)):
            text = parse(d)
            if text is None:
                continue
            if GBUS_LINE.match(text):
                continue
            last, at = text, pos
        if not last or (not self._is_leader(sender) and not TRANSFER.search(last[:400])):
            return None, None, "The user must say transfer <recipient> to start a conversation"
        key = (path, at, target["tty"], target["pid"])
        mode = "dialog" if not self._is_leader(sender) and DIALOG.search(last[:400]) else "relay"
        return key, mode, None

    def _session_log(self, s, session):
        if s.get("log"):
            return s["log"]
        agent = s.get("agent")
        if session and agent == "claude":
            hits = glob.glob(str(CLAUDE_PROJECTS / "*" / f"{session}.jsonl"))
        elif session and agent == "codex":
            hits = glob.glob(str(CODEX_SESSIONS / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"))
        elif agent == "grok" and s.get("cwd"):
            # у Grok нет id сессии в окружении: самый свежий журнал этой папки
            hits = sorted((p for p in glob.glob(str(GROK_SESSIONS / "*" / "*" / "chat_history.jsonl"))
                           if os.path.realpath(unquote(Path(p).parent.parent.name)) == os.path.realpath(s["cwd"])),
                          key=os.path.getmtime, reverse=True)
        else:
            hits = []
        return hits[0] if hits else None

    def _new_thread(self, sender, target, text, mode="relay", topic=None, turns=None):
        # 9-character channel ID; message h1 is 11 characters instead of 35.
        # Exclusive reservation survives restarts and prevents reuse in archived logs.
        ids = self.journal.parent / "orch" / "ids"
        ids.mkdir(parents=True, exist_ok=True)
        while True:
            tid = "t" + secrets.token_urlsafe(6)
            with self.lock:
                if tid in self.threads:
                    continue
                try:
                    with (ids / tid).open("x"):
                        pass
                except FileExistsError:
                    continue
                break
        side = lambda s: {k: s.get(k) for k in ("tty", "pid", "agent", "name", "nick", "log", "terminal_app")}
        th = {"id": tid, "a": side(sender), "b": side(target), "hop": 0, "status": "active",
              "queue": [], "pending": {}, "started": time.time(), "last_text": None, "mode": mode,
              "leader": self._session_id(sender) if self._is_leader(sender) else None,
              "topic": " ".join(str(topic or text).split())[:160], "phase": "accepted", "last_message": None,
              "turn_limit": turns or self.max_hops(), "stop_reason": None}
        if mode == "channel":
            for side_name in ("a", "b"):
                tab = th[side_name]
                tab["birth"] = _process_start(tab["pid"])
                if not tab["birth"]:
                    raise ValueError("Could not verify the terminal process")
        kind = {"channel": "persistent channel", "dialog": "conversation", "relay": "relay"}[mode]
        self._note("info", f"Transfer ({kind}): {sender['nick']} ({sender['name']}) → {target['nick']} ({target['name']})",
                   thread=tid)
        with self.lock:
            if text is None:
                th.update(addressed=True, phase="open")
                for key in ("a", "b"):
                    guard.attach(th, key, th[key]["log"])
                    watch = th.get("watches", {}).get(key)
                    if not watch:
                        raise ValueError("Both participants need readable Codex or Claude logs")
                    watch["addressed"] = True
                    for active in self.threads.values():
                        if active.get("addressed") and active["status"] == "active":
                            for participant, existing in active.get("watches", {}).items():
                                if self._session_id(active[participant]) == self._session_id(th[key]) and existing.get("terminal_mode"):
                                    watch["terminal_mode"] = existing["terminal_mode"]
                if th["watches"]["a"]["path"] == th["watches"]["b"]["path"]:
                    raise ValueError("Both participants share a log")
                self._save_channel(th)
            self.threads[tid] = th
            if text is not None:
                self._enqueue(th, th["a"], th["b"], text)
        return th

    @staticmethod
    def _session_id(side):
        return f"{side['agent']}:{side['pid']}:{side['tty']}"

    def _enqueue(self, th, frm, to, text, final=False, reply_to=None, recovered=False, notice=False, relay_to=None, author_role="assistant", audience=None):
        """Полный конверт в Voice, короткая однострочная доставка в терминал."""
        with self.lock:
            th["hop"] += 1
            mid = f"{th['id']}h{th['hop']}"
            kind = "message" if th["mode"] == "channel" else "complete" if final else "reply" if reply_to else "request"
            msg = {"at": time.strftime("%H:%M:%S"), "v": 2, "id": mid, "message": mid, "channel": th["id"], "thread": th["id"],
                   "seq": th["hop"], "hop": th["hop"], "reply_to": reply_to, "type": kind,
                   "topic": th["topic"], "mode": th["mode"], "expects_reply": not final and th.get("mode") != "channel", "final": final,
                   "from": self._session_id(frm), "to": self._session_id(to),
                   "from_terminal": frm["tty"].removeprefix("/dev/"),
                   "to_terminal": to["tty"].removeprefix("/dev/"),
                   "from_name": frm["nick"], "to_name": to["nick"], "body": text,
                   "leader": th.get("leader"), "delegated_by_leader": th.get("leader") == self._session_id(frm)}
            if th.get("mode") == "channel":
                origin = "a" if msg["from"] == self._session_id(th["a"]) else "b"
                counters = th.setdefault("next_number", {"a": 1, "b": 2})
                msg["number"] = counters[origin]
                counters[origin] += 2
                msg["recovered"] = recovered
            path = self.journal.parent / "orch" / f"{mid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)

            reply_token = secrets.token_urlsafe(6) if not final and th.get("mode") != "channel" else None
            reply_id = None
            if reply_token:
                self._reply_seq = getattr(self, "_reply_seq", 0) + 1
                reply_id = f"r{self._reply_seq}"
            if th.get("addressed"):
                msg.update(addressed=True, audience=audience, notice=notice, author_role="service" if notice else author_role)
                if author_role == "user":
                    msg["from_name"] = "You from " + msg["from_terminal"].removeprefix("ttys")
            if relay_to:
                msg["relay_to"] = relay_to["tty"].removeprefix("/dev/")
            path.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
            line = _message_line(msg)
            if relay_to:
                th.setdefault("relay_pending", {})[mid] = {"line": line, "body": text, "source": frm["tty"]}
            th["queue"].append({"to": to, "frm": frm, "msg": msg, "line": line,
                                "reply_token": reply_token, "reply_id": reply_id, "final": final, "at": time.time()})
            th["last_message"], th["phase"] = mid, "queued"
            self._save_channel(th)
            self._note("message", text, thread=th["id"], channel=th["id"], hop=th["hop"],
                       frm=msg["from_name"], to=to["nick"], final=final, envelope=msg)
            self.wake.set()

    # ---------- рабочие потоки: доставка, ожидание ответов, запросы файлом

    def _run(self):
        while not self._stop.is_set():
            self.wake.wait(0.5)
            self.wake.clear()
            try:
                self._step()
            except Exception as e:
                self.log.exception("Gateway: %s", e)

    def _run_spool(self):
        while not self._stop.is_set():
            try:
                self._spool()
            except Exception as e:
                self.log.warning("Gateway spool: %s", e)
            self._stop.wait(0.3)

    def _spool(self):
        """Запросы gbus файлом (агент в песочнице без сети): SPOOL/<id>.req.json → ответ <id>.res.json."""
        for req in glob.glob(os.path.join(SPOOL, "*.req.json")):
            try:
                with open(req, encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(req)
            except (OSError, ValueError):
                continue
            body, path = data.get("body") or {}, data.get("path")
            try:
                out = {"send": self.public_send, "end": self.end, "bind": self.bind, "whoami": self.whoami, "reply": self.reply, "read": self.read, "leader-ready": self.ready_leader}.get(path, lambda b: self.state())(body)
            except Exception as e:
                out = {"ok": False, "error": str(e)}
            try:
                tmp = req.replace(".req.json", ".tmp-res")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False)
                os.rename(tmp, req.replace(".req.json", ".res.json"))
            except OSError as e:
                self.log.warning("Gateway spool: %s", e)
        for res in glob.glob(os.path.join(SPOOL, "*.res.json")):     # ответы, которые gbus так и не забрал
            try:
                if time.time() - os.path.getmtime(res) > 300:
                    os.remove(res)
            except OSError:
                pass

    @staticmethod
    def _ready_item(th):
        now = time.time()
        seen = set()
        for item in th["queue"]:
            tty = item["to"]["tty"]
            if tty not in seen and now >= item.get("retry_at", 0):
                return item
            seen.add(tty)
            if th.get("mode") != "channel":
                break
        return None

    def _step(self):
        self._ui_start()
        with self.lock:
            has_open_channels = any(th.get("mode") == "channel" and th["status"] in ("active", "suspended")
                                    for th in self.threads.values())
        if has_open_channels:
            self.sessions()
        with self.lock:
            ths = [th for th in self.threads.values() if th["status"] == "active"]
        for th in ths:
            self._poll_reply(th)
            with self.lock:
                if self._ready_item(th) and not self.paused and th["id"] not in self.deliveries and th["status"] == "active":
                    self.deliveries.add(th["id"])
                    threading.Thread(target=self._dispatch, args=(th,), name="gbus-deliver", daemon=True).start()

    def _dispatch(self, th):
        try:
            while not self._stop.is_set():
                with self.lock:
                    if self.paused or th["status"] != "active" or not th["queue"]:
                        return
                if self._deliver(th) is False:
                    return
        except Exception as e:
            self.log.exception("Gateway: delivering %s", th["id"])
            self._fail(th, f"Delivery failed: {e}")
        finally:
            with self.lock:
                self.deliveries.discard(th["id"])
            self.wake.set()

    def _ui_start(self):
        with self.lock:
            item = self.ui_starts[0] if self.ui_starts and not self.paused else None
        if not item:
            return
        side = item["side"]
        cur = self._current(side)
        if cur and cur["pid"] == side["pid"] and _idle(cur):
            try:
                log = cur.get("log")
                size = os.path.getsize(log) if log else 0
                self._type(side, item["text"])
                self._confirm_submit(side, log, size)
                self._note("info", f"Typed opening message: {item['label']}")
            except Exception as e:
                self._note("error", f"Opening message failed: {e}")
        elif time.time() - item["at"] < 120:
            return                                          # вкладка занята — подождём
        else:
            self._note("error", f"Opening message: {side['nick']} stayed busy for 2 minutes")
        with self.lock:
            if self.ui_starts and self.ui_starts[0] is item:
                self.ui_starts.pop(0)

    def _confirm_submit(self, side, log, size):
        """Запрос ушёл, если в журнале появилась новая строка пользователя; нет — и агент свободен — Enter ещё раз."""
        if not log:
            return
        parse = USER_TEXT.get(side["agent"])
        if parse is None:
            return
        for _ in range(ENTER_RETRIES):
            time.sleep(ENTER_CHECK)
            if any(parse(d) is not None for _, d in _read_jsonl(log, size)):
                return
            cur = next((x for x in self.sessions(fresh=True) if x["tty"] == side["tty"]), None)
            if not cur or cur.get("state") != "idle":
                return
            self.log.info("Gateway: %s did not submit. Sending Enter again", side["nick"])
            self._enter(side)

    def _current(self, side):
        return next((x for x in self.sessions() if x["tty"] == side["tty"]), None)

    def _fail(self, th, text):
        with self.lock:
            if th.get("mode") == "channel":
                th["status"], th["phase"] = "suspended", "delivery_error"
            else:
                th["queue"].clear()
                th["pending"].clear()
                th["status"], th["phase"] = "failed", "error"
            th["stop_reason"] = text
            self._save_channel(th)
        self._note("error", text, thread=th["id"], message=th.get("last_message"))

    def _deliver(self, th):
        if self._is_leader(th["a"]) or self._is_leader(th["b"]):
            self._fail(th, "The participant became the lead. GBus delivery stopped")
            return
        with self.lock:
            if not th["queue"] or th["status"] != "active":
                return
            item = self._ready_item(th)
            if item is None:
                return False
            to = item["to"]
            write_lock = self.write_locks.setdefault(to["tty"], threading.Lock())
        # Ждём только завершения физической вставки соседнего письма, никогда — ответа CLI.
        with write_lock:
            with self.lock:
                if th["status"] != "active" or item not in th["queue"]:
                    return
            cur = self._current(to)
            if not cur and th.get("mode") == "channel" and _pid_alive(to["pid"]):
                item["retry_at"] = time.time() + 2
                return False
            if not cur or cur["pid"] != to["pid"] or (to.get("birth") and _process_start(cur["pid"]) != to["birth"]) or (to.get("log") and cur.get("log") and to["log"] != cur["log"]):
                self._fail(th, f"{to['nick']}: session closed or changed")
                return
            if to["agent"] == "codex":
                with self.lock:
                    occupied = any(w["side"]["tty"] == to["tty"] for thread in self.threads.values()
                                   for w in thread.get("pending", {}).values())
                busy = bool(cur.get("log") and _codex_state(cur["log"]) == "busy")
                if occupied or busy:
                    if th.get("mode") != "channel" and time.time() - item["at"] > self.reply_timeout():
                        self._fail(th, "Delivery timed out while the terminal was busy with another turn")
                    else:
                        if th["phase"] != "waiting_terminal":
                            self._note("waiting", "Waiting for the terminal to become available", thread=th["id"], message=item["msg"]["id"], terminal=to["tty"])
                        th["phase"] = "waiting_terminal"
                        item["retry_at"] = time.time() + 1
                    return False
            w = {"side": to, "other": item["frm"], "marker": item["msg"]["id"],
                 "since": time.time(), "reply_token": item.get("reply_token"), "reply_id": item.get("reply_id"), "log": cur.get("log")}
            tracked = not item["final"] and th.get("mode") != "channel"
            if tracked and to["agent"] == "codex":
                from gateway_turns import TurnTracker
                w["tracker"] = TurnTracker(self._codex_log_paths, item["line"], w["since"])
                w["stage"] = "delivered"
            with self.lock:
                if th.get("mode") == "channel":
                    if th.get("addressed"):
                        for active in self.threads.values():
                            if active["status"] == "active":
                                for key, watch in active.get("watches", {}).items():
                                    if self._session_id(active[key]) == self._session_id(to):
                                        receipts = watch.setdefault("voice_inputs", {})
                                        receipts[item["line"]] = receipts.get(item["line"], 0) + 1
                                        roles = watch.setdefault("input_audiences", {})
                                        if item["msg"].get("audience") or item["line"] in roles:
                                            roles.setdefault(item["line"], []).append(item["msg"].get("audience"))
                                        self._save_channel(active)
                    if not th.get("addressed") and not item["msg"].get("recovered"):
                        key = "a" if self._session_id(to) == self._session_id(th["a"]) else "b"
                        guard.attach(th,key,cur.get("log"))
                        watch = th.get("watches", {}).get(key)
                        if watch:
                            watch["expected"][item["msg"]["id"]] = guard.signature(item["line"])
                    th["in_flight"] = item["msg"]["id"]
                    self._save_channel(th)
                if tracked:
                    th["pending"][w["marker"]] = w
            try:
                self._type(to, item["line"])
            except Exception as e:
                self._fail(th, f"Could not deliver to {to['nick']}: {e}")
                return
            with self.lock:
                if th["status"] != "active" or item not in th["queue"]:
                    return
                th["queue"].remove(item)
                th.pop("in_flight", None)
                th["phase"] = "open" if th.get("mode") == "channel" else "answer_delivered" if item["final"] else "delivered"
                if item["final"] and not th["pending"] and not th["queue"]:
                    th["status"] = "done"
                self._save_channel(th)
            self._note("delivered", f"→ {to['nick']}", thread=th["id"], message=item["msg"]["id"], hop=item["msg"]["hop"])
            if item["final"] and not th.get("leader"):
                self._muted.pop(cur.get("log"), None)
            else:
                self._mute(cur.get("log"))

    def _type(self, side, text):
        """Текст во вкладку Terminal по tty и Enter — окна и фокус не трогаем.
        Печатаем, только если агент жив и на переднем плане вкладки: иначе строку выполнил бы shell."""
        tty = side["tty"]
        if not _foreground(tty, side["pid"]):
            raise RuntimeError(f"The agent is not active in tab {tty}. Input was skipped")
        script = ITERM_WRITE_SCRIPT if side.get("terminal_app") == ITERM else DO_SCRIPT
        if _osascript(script % (_as_string(tty), _as_string(text))).strip() != "ok":
            raise RuntimeError(f"Tab {tty} is closed")
        time.sleep(ENTER_GAP + min(1.5, len(text) / 3000))   # длинный текст агент дольше принимает как вставку
        self._enter(side)

    @staticmethod
    def _enter(side):
        if not _foreground(side["tty"], side["pid"]):
            raise RuntimeError(f"The agent is not active in tab {side['tty']}")
        # Пустой do script не отправляет Return в новом Codex. Явный CR идёт
        # через AppleEvent точно в tty, не выбирая вкладку/окно и не меняя фокус.
        script = ITERM_WRITE_SCRIPT if side.get("terminal_app") == ITERM else DO_SCRIPT
        if _osascript(script % (_as_string(side["tty"]), '(character id 13)')).strip() != "ok":
            raise RuntimeError("The terminal closed before Enter was sent")

    def _codex_log_paths(self):
        now = time.monotonic()
        if now - getattr(self, "_auto_paths_at", 0) > 1:
            self._auto_paths = glob.glob(str(CODEX_SESSIONS / "*" / "*" / "*" / "rollout-*.jsonl"))
            self._auto_paths_at = now
        return self._auto_paths

    def _guard_channel(self, th):
        # Only messages tied to a verified receipt can leave this transcript.
        with self.lock:
            if th["status"] != "active":
                return
            for key, watch in th.get("watches", {}).items():
                side = th[key]
                other = th["b" if key == "a" else "a"]
                if th.get("addressed"):
                    routes = []
                    for candidate in self.threads.values():
                        if candidate.get("addressed") and candidate["status"] == "active":
                            for src, dst in (("a", "b"), ("b", "a")):
                                if self._session_id(candidate[src]) == self._session_id(side):
                                    routes.append((candidate["id"], candidate[dst]))
                    def addressed(token, text):
                        if token.startswith("relay:"):
                            mid = token[6:]
                            pending = th.get("relay_pending", {}).get(mid)
                            if pending and pending["source"] == side["tty"] and pending["line"] == text:
                                del th["relay_pending"][mid]
                                self._enqueue(th, side, other, pending["body"], reply_to=mid, author_role="user")
                            return
                        preferred = watch.get("terminal_mode")
                        if preferred in (None, "чат"):
                            preferred = max((cid for cid, _ in routes), key=lambda cid: self.threads[cid].get("selected_at", self.threads[cid]["started"]))
                        channel, body = guard.destination(text, routes)
                        if token.startswith("addressed-user:"):
                            if channel == th["id"]:
                                self._enqueue(th, side, other, body, author_role="user", audience="remote_agent")
                            return
                        if not guard.ADDRESS.match(text) and not re.match(r"^(?:Синьер,\s*)?\[(?:to|dialog|кому|диалог):", text, re.I):
                            context = watch.get("reply_channel")
                            if context:
                                channel, body = context, text
                            else:
                                channel, body = preferred, text
                        if channel == th["id"]:
                            digest = guard.signature(body)
                            if watch.get("notice_input") and watch.get("last_body") == digest:
                                return
                            watch["last_body"] = digest
                            self._enqueue(th, side, other, body, audience="terminal_user" if watch.get("reply_to_terminal_user") and watch.get("reply_channel") == channel else None)
                        elif channel is None and th["id"] == routes[0][0]:
                            choices = ", ".join("[to:" + p["tty"].split("ttys")[-1] + "]" for _, p in routes)
                            hint = f"{guard.NOTICE} Repeat the message with one address at the start: {choices}. "
                            hint += "If the message was private, ignore this notice. Message preview: " + text[:140]
                            self._enqueue(th, side, side, hint, notice=True)
                    if guard.poll(watch, False, addressed):
                        self._save_channel(th)
                    if watch.get("invalid"):
                        self._fail(th, "The terminal log changed. End the conversation and select the pair again.")
                    continue
                routes = sum(1 for t in self.threads.values()
                             if t.get("mode") == "channel" and t["status"] == "active"
                             and any(self._session_id(t[k]) == self._session_id(side) for k in ("a","b")))
                def deliver(tid, text):
                    tag = tid + ":" + guard.signature(text)
                    if tag in watch.get("sent", {}):
                        return
                    self._enqueue(th, side, other, text, recovered=True)
                    watch.setdefault("mirrored", {})[tag] = th["last_message"]
                    self._note("recovered_reply", "Delivering local reply through the channel",
                               thread=th["id"], message=th["last_message"], turn_id=tid)
                if guard.poll(watch, routes == 1, deliver):
                    self._save_channel(th)

    def _poll_reply(self, th):
        if th.get("mode") == "channel":
            self._guard_channel(th)
            return
        with self.lock:
            pending = list(th["pending"].items())
        for mid, w in pending:
            tracker = w.get("tracker")
            result = tracker.poll() if tracker else None
            with self.lock:
                # Explicit reply and automatic completion share this exact-once gate.
                if th["status"] != "active" or th["pending"].get(mid) is not w:
                    continue
                current = self._current(w["side"])
                reason = None
                if not current or current["pid"] != w["side"]["pid"] or not _pid_alive(w["side"]["pid"]):
                    reason = "The recipient closed or switched sessions"
                elif result and result["stage"] == "error":
                    reason = result["reason"]
                elif (w.get("tracker") and not w.get("turn_id") and time.time() - w["since"] > float(self.cfg.get("orch_receive_timeout", 120))):
                    reason = "Timed out waiting for the agent to accept the delivered message"
                elif time.time() - w.get("received_at", w["since"]) > self.reply_timeout():
                    reason = "Timed out: " + ("the agent did not accept the message" if w.get("stage", "delivered") == "delivered" else "the agent did not finish the reply")
                if reason:
                    del th["pending"][mid]
                    th["stop_reason"] = reason
                    self._note("error", reason, thread=th["id"], message=mid, turn_id=w.get("turn_id"))
                    continue
                if result and result["stage"] in ("accepted", "completed"):
                    if not w.get("turn_id"):
                        w.update(turn_id=result["turn_id"], log=result["path"], stage="processing", received_at=time.time())
                        th["phase"] = "processing"
                        self.tab_logs[(current["tty"], current["pid"])] = result["path"]
                        if result.get("session"):
                            self._remember_sender(result["session"], current)
                        self._note("received", "The agent accepted the message", thread=th["id"], message=mid,
                                   turn_id=result["turn_id"], terminal=current["tty"])
                        self._mute(result["path"])
                    if result["stage"] == "completed":
                        del th["pending"][mid]
                        self._note("completed", "Reply ready", thread=th["id"], message=mid, turn_id=result["turn_id"])
                        self._on_reply(th, w["side"], w["other"], result["answer"], reply_to=mid)
        with self.lock:
            if not th["pending"] and not th["queue"] and th["status"] == "active":
                th["status"], th["phase"] = "failed", "error"
                th["stop_reason"] = th.get("stop_reason") or "No pending message or reply"

    def _on_reply(self, th, frm, to, text, reply_to=None):
        if self._is_leader(frm) or self._is_leader(to):
            self._fail(th, "The participant became the lead. GBus conversation stopped")
            return
        text = text.strip()
        th["phase"] = "answered"
        self._note("reply", text, thread=th["id"], hop=th["hop"], frm=frm["nick"], to=to["nick"], reply_to=reply_to)
        if not text:
            self._fail(th, f"{frm['nick']}: the turn ended without a reply. Conversation stopped")
            return
        # Codex answers arrive through its normal speech source. Explicit
        # replies from other CLIs may exist only in gbus, without a final message.
        if frm.get("agent") != "codex":
            try:
                self.on_leader_answer(frm, text)
            except Exception as exc:
                self.log.warning("Reading reply to the lead: %s", exc)
        marked_done = bool(DONE_RE.search(text))
        done = marked_done or th.get("mode") == "relay"   # пересылка: один вопрос — один ответ
        limit = th.get("turn_limit", self.max_hops())
        final = done or th["hop"] + 1 >= limit or text == th["last_text"]
        th["last_text"] = text
        greet = self.cfg.get("orch_strip_greetings", GREETINGS) if self.cfg.get("orch_strip", True) else ""
        if not th.get("leader"):
            text = _clean(text, greet)                       # собеседнику — без обращения (если включено) и [DONE]
        if final:
            th["stop_reason"] = "closed by the other participant" if marked_done else "reply received" if th.get("mode") == "relay" else "turn limit" if th["hop"] + 1 >= limit else "duplicate reply"
        if final and not done:
            self._note("info", "turn limit" if th["hop"] + 1 >= limit else "duplicate reply; stopped",
                       thread=th["id"])
        with self.lock:
            if th["status"] == "active":
                self._enqueue(th, frm, to, text, final=final, reply_to=reply_to)

    # ---------- озвучка: ответы диалога агентов вслух не читаем

    def _mute(self, path, grace=MUTE_GRACE):
        if path:
            self._muted[path] = max(self._muted.get(path, 0), time.time() + grace)

    def muted(self, path):
        """Для AnswerWatcher: событие из журнала, который сейчас в диалоге шлюза. Никогда не бросает."""
        try:
            if not path:
                return False
            path = str(path)
            with self.lock:
                for th in self.threads.values():
                    if any(w.get("log") == path for w in th["pending"].values()):
                        return True
            return self._muted.get(path, 0) > time.time()
        except Exception:
            return False

    # ---------- лента и журнал

    def _note(self, kind, text, **extra):
        rec = {"at": time.strftime("%H:%M:%S"), "ts": round(time.time(), 3), "kind": kind, "text": text, **extra}
        with self.lock:
            self.feed.append(rec)
            del self.feed[:-300]
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            self.log.warning("Gateway log: %s", e)
        self.log.info("Gateway [%s] %s", kind, (text or "")[:200])

    def state(self):
        sess = self.sessions()
        with self.lock:
            threads = []
            for th in sorted(self.threads.values(), key=lambda x: -x["started"]):
                waiting = list(th["pending"].values())
                threads.append({"id": th["id"], "a": th["a"]["nick"], "b": th["b"]["nick"],
                                "a_tty": th["a"]["tty"], "b_tty": th["b"]["tty"], "a_name": th["a"]["name"], "b_name": th["b"]["name"], "hop": th["hop"],
                                "max": th.get("turn_limit", self.max_hops()), "stop_reason": th.get("stop_reason"), "status": th["status"], "topic": th["topic"],
                                "mode": th["mode"], "phase": th["phase"], "last_message": th["last_message"],
                                "waiting": ", ".join(sorted({w["side"]["nick"] for w in waiting})) or None,
                                "pending": [{"message": w["marker"], "to": w["side"]["nick"], "stage": w.get("stage", "delivered"), "turn_id": w.get("turn_id"), "waiting_seconds": int(time.time()-w["since"])} for w in waiting], "queued": len(th["queue"])})
            return {"enabled": self.enabled(), "paused": self.paused,
                    "excluded_terminals": list(self.cfg.get("orch_leader_excluded_ttys", [])),
                    "leader": {k: self.leader_session.get(k) for k in ("session_id", "tty", "pid", "nick")} if self.leader_session else None,
                    "sessions": [{k: s.get(k) for k in ("id", "session_id", "nick", "name", "agent", "tty", "cwd", "state", "terminal_index", "description", "log")} for s in sess],
                    "threads": threads, "finished": sorted(self.finished_channels.values(), key=lambda x: -x["ended"]), "feed": self.feed[-150:]}


# ---------- текст реплики для собеседника

GREETINGS = "Jonny"   # по умолчанию; список — настройка orch_strip_greetings
DONE_TAIL = re.compile(r"\s*[*_`]*\[DONE\][*_`]*[\s.!»\"')]*$")


def _clean(text, greetings=GREETINGS):
    """Обращение из правил агента («Синьер,», «Амиго,»…) и метка [DONE] — для пользователя, не для собеседника.
    greetings — через запятую (настройка «Срезать обращения»). В журнал шлюза реплика пишется как есть."""
    words = [w.strip() for w in str(greetings or "").split(",") if w.strip()]
    if words:
        lead = re.compile(r"^\s*(" + "|".join(re.escape(w) for w in words) + r")\b[\s,!.:;—–-]*", re.I)
        text2 = lead.sub("", text, count=1)
    else:
        text2 = text
    out = DONE_TAIL.sub("", text2).strip()
    return out or text


# ---------- процессы вкладок

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _tty_agent(tty):
    """(pid, агент) первого агента во вкладке; None — агента нет; False — ps не ответил."""
    try:
        out = subprocess.run(["ps", "-t", tty[5:], "-o", "pid=,args="], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        return False
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            argv = shlex.split(parts[1])
        except ValueError:
            continue
        if not argv:
            continue
        exe = os.path.basename(argv[0]).lower()
        if exe in AGENTS:
            return int(parts[0]), exe
        # Node/Bun запускают CLI как скрипт; ищем только имя скрипта, не аргументы запросов.
        if exe in ("node", "bun", "nodejs") and len(argv) > 1 and not argv[1].startswith("-"):
            script = argv[1]
            name = os.path.basename(script).lower()
            if name in AGENTS:
                return int(parts[0]), name
            if name in ("cli.js", "index.js") and "/pi-coding-agent/" in script:
                return int(parts[0]), "pi"
            if name in ("cli.js", "index.js") and "/opencode" in script:
                return int(parts[0]), "opencode"
    return None


def _foreground(tty, pid):
    """Агент жив и на переднем плане вкладки (stat с «+»): только тогда печатать во вкладку безопасно."""
    try:
        out = subprocess.run(["ps", "-t", tty[5:], "-o", "pid=,stat="], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pid):
            return "+" in parts[1]
    return False


def _idle(s):
    """Свободен ли агент: Claude — по файлу сессии, Codex — по журналу; неизвестно (Grok, новый Codex) — да."""
    return s.get("state") in ("", "idle")


def _codex_activity(path):
    """Read lifecycle records, never infer work from response text or cwd."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            lines = f.read().splitlines()
    except OSError:
        return {"state": "", "work_id": ""}
    state, work_id = ("idle" if size < 2_000_000 else ""), ""
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get("type") != "event_msg":
            continue
        payload = item.get("payload") or {}
        kind = payload.get("type")
        if kind == "task_started":
            state = "busy"
            work_id = str(payload.get("turn_id") or item.get("timestamp") or "")
        elif kind in ("task_complete", "turn_aborted"):
            state = "idle"
    return {"state": state, "work_id": work_id}


def _codex_state(path):
    return _codex_activity(path)["state"]


def _tty_of(pid):
    """tty вкладки по pid процесса gbus: вверх по родителям (gbus в песочнице сам ps запустить не может)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=3).stdout.split()
        except Exception:
            return None
        if len(out) < 2:
            return None
        if out[1] not in ("??", "-"):
            return "/dev/" + out[1]
        pid = int(out[0])
        if pid <= 1:
            return None
    return None


def _parse_process_identity(raw):
    """KERN_PROCARGS2: argc, путь программы, argv, затем NUL-разделённое окружение.
    Разбираем окружение отдельно от argv: текст письма не может подделать переменную.
    """
    if len(raw) < 5:
        return {}
    argc = int.from_bytes(raw[:4], "little")  # macOS Intel и Apple Silicon
    if not 1 <= argc <= 10000:
        return {}
    end = raw.find(b"\0", 4)
    if end < 0:
        return {}
    pos = end + 1
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    for _ in range(argc):
        end = raw.find(b"\0", pos)
        if end < 0:
            return {}
        pos = end + 1
    keys = {b"OPENCODE_SESSION_ID", b"OPENCODE_TERMINAL", b"TERM_SESSION_ID", b"CODEX_THREAD_ID"}
    identity = {}
    for entry in raw[pos:].split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key in keys:
            try:
                identity[key.decode("ascii")] = value.decode("ascii")
            except UnicodeDecodeError:
                return {}
    return identity


def _process_identity(pid):
    """Из окружения живого процесса берём только три ID; остальное нигде не выводим."""
    try:
        import ctypes
        pid = int(pid)
        if pid <= 1:
            return {}
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, PID
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 0 < size.value <= 8 * 1024 * 1024:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            return {}
        return _parse_process_identity(buf.raw[:size.value])
    except (TypeError, ValueError, OSError, AttributeError, OverflowError):
        return {}


def _message_line(msg):
    preview = " ".join(msg["body"].split())
    if msg.get("addressed"):
        # Preserve the full body in the envelope. Keep terminal input on one physical line.
        preview = "".join(ch for ch in preview if ch.isprintable())
        if msg.get("relay_to"):
            return (f"[Voice · relay {msg['id']}] Relay the exact text to {msg['relay_to']}. "
                    "Voice forwards it automatically when you receive it. "
                    "The other participant handles the assignment. End this turn without a reply. Text: " + preview)
        if msg.get("notice"):
            return preview
        terminal = msg["from_terminal"].removeprefix("ttys")
        label = f"You from {terminal}" if msg.get("author_role") == "user" else f"Agent {terminal}"
        return f"[{label}][dialog:{msg['channel']}] {preview}"
    if len(preview) > LONG_TEXT:
        preview = preview[:300] + "…"
    sender = " ".join(str(msg.get("from_name") or "").split()).replace("[", "").replace("]", "").strip()
    label = f"{msg['id']} {sender}" if sender else msg["id"]
    if msg.get("delegated_by_leader") and not msg.get("final"):
        # Routing metadata is separate from body; an ex-leader must read it
        # before interpreting this as a request to relay to a third terminal.
        role_hint = (f"[Voice: run gbus read {msg['id']}; "
                     "check your current role because the lead assignment may have changed] ")
    else:
        role_hint = ""
    if msg.get("mode") == "channel":
        role_hint = (f"[channel {msg['channel']}, #{msg.get('number', msg['seq'])}; send updates and the result with gbus send --channel {msg['channel']}; "
                     "continue here after the user replies] ")
    return f"[gbus {label}] {role_hint}{preview}"


def _process_start(pid):
    try:
        result = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="],
                                env=dict(os.environ, LC_ALL="C"), capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return ""


def _managed_codex_parent(ancestors):
    pids = [str(p) for p in ancestors if isinstance(p, int) and p > 1]
    if not pids:
        return False
    try:
        rows = subprocess.run(["ps", "-p", ",".join(pids), "-o", "command="],
                              capture_output=True, text=True, timeout=3)
        if rows.returncode:
            return True  # uncertainty must not route via the daemon's inherited tty
        return any("app-server" in row and ("--managed-daemon" in row or "daemon pid-update-loop" in row)
                   for row in rows.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return True


def _opencode_parent(ancestors):
    """Инструмент должен быть настоящим потомком процесса OpenCode, включая фоновый сервер."""
    pids = [str(p) for p in ancestors if isinstance(p, int) and p > 1]
    if not pids:
        return False
    try:
        result = subprocess.run(["ps", "-p", ",".join(pids), "-o", "comm="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(os.path.basename(line.strip()) == "opencode" for line in result.stdout.splitlines())


def _ancestor_pids(pid):
    """Find the actual agent even if its tool allocated a different pseudo-terminal."""
    try:
        pid = int(pid)
        rows = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                              text=True, timeout=3).stdout.splitlines()
        parents = {int(parts[0]): int(parts[1]) for row in rows if len(parts := row.split()) == 2}
    except (TypeError, ValueError, OSError, subprocess.TimeoutExpired):
        return []
    result = []
    while pid > 1 and pid not in result and len(result) < 64:
        result.append(pid)
        pid = parents.get(pid, 0)
    return result


def _codex_open_log(pid):
    """Unique rollout held by this exact live TUI, not its shared server/cwd.

    Re-scan on every terminal inventory so a resumed conversation replaces the
    previous log. PID birth checks reject a process replaced during discovery.
    """
    birth = _process_start(pid)
    if not birth:
        return None
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-Fn"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return None
        root = CODEX_SESSIONS.resolve()
        paths = set()
        for line in result.stdout.splitlines():
            if not line.startswith("n/"):
                continue
            path = Path(line[1:])
            if (re.fullmatch(r"rollout-.*-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl", path.name)
                    and path.resolve().is_relative_to(root) and path.is_file()):
                paths.add(str(path.resolve()))
        if len(paths) == 1 and _process_start(pid) == birth:
            return paths.pop()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _grok_open_log(pid):
    """Use the events file actually held by Grok, never the newest cwd log."""
    birth = _process_start(pid)
    if not birth:
        return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-Fn"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode:
            return None
        root = GROK_SESSIONS.resolve()
        paths = set()
        for line in out.stdout.splitlines():
            if not line.startswith("n/"):
                continue
            path = Path(line[1:]).resolve()
            if path.name == "events.jsonl" and path.is_relative_to(root):
                history = path.with_name("chat_history.jsonl")
                if history.is_file():
                    paths.add(str(history))
        if len(paths) == 1 and _process_start(pid) == birth:
            return paths.pop()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None
