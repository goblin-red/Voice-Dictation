"""Команда «Активный терминал»: помнит последний терминал (и его вкладку), где вы работали, и возвращает в него.

Запоминаем, уходя из терминала в другое приложение (раз в секунду смотрим, кто в фокусе),
и когда Voice сам печатает во вкладку. Окно Voice, Goblin Remote и посторонние приложения запись не стирают.

Что помним:
  Orca      — адрес вкладки (handle) и её панель (вкладка + панель + рабочая область);
  Terminal  — tty вкладки;
  iTerm2    — id сессии (панели);
  другие    — само приложение: свою вкладку оно помнит само.
Цель закрыта — так и говорим, другой терминал взамен не выбираем.
"""

import json
import os
from pathlib import Path
import threading
import time

import direct
import orca_cli
from i18n import t

TERMINAL = "com.apple.Terminal"

# ближайшее (по порядку окон) окно Terminal, где выбрана не вкладка Джони: её tty или ""
OTHER_TAB = '''
tell application "Terminal"
  repeat with w in windows
    set t to selected tab of w
    if tty of t is not %s then return tty of t
  end repeat
  return ""
end tell'''
ALL_TERMINALS = (direct.ORCA_APP, *direct.TERMINAL_APPS)


class LastTerminal:
    def __init__(self, direct_, log, state_path=None):
        self.direct = direct_
        self.log = log
        self.record = None      # последняя терминальная цель: {"kind": orca|tab|app, "bundle", "pid", ...}
        self.speech_focus_lost = False  # Input focus survives covering applications.
        self.current = None     # терминал, который сейчас впереди (NSRunningApplication) или None
        self.skip_tab = lambda: None   # tty вкладки Джони — «последним терминалом» её не считаем
        self._lock = threading.Lock()
        self.state_path = Path(state_path) if state_path is not None else Path(__file__).resolve().parent.parent / "data" / "state" / "last-terminal.json"
        self._load()

    def _load(self):
        try:
            rec = json.loads(self.state_path.read_text())
            if not isinstance(rec, dict) or rec.get("bundle") not in ALL_TERMINALS or rec.get("kind") not in ("app", "tab", "orca"):
                return
            if rec["kind"] == "tab" and not isinstance(rec.get("key"), str):
                return
            if rec["kind"] == "orca" and not rec.get("handle"):
                return
            if rec["kind"] != "orca" and self.direct.app_by_pid(rec.get("pid"), rec["bundle"]) is None:
                return  # process ended/restarted: never substitute another terminal
            self.record = rec
            if rec["kind"] != "orca":
                app = self.direct.app_by_pid(rec.get("pid"), rec["bundle"])
                self.direct.last_terminal = app
                self.direct.last_app = app
            self.log.debug("Restored the active terminal after restarting Voice")
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.log.warning("Could not read the saved active terminal: %s", exc)

    def _save(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.record, ensure_ascii=False))
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self.log.warning("Could not save the active terminal: %s", exc)

    def checkpoint(self):
        app = self.direct.front_app()
        if app is not None and app.bundleIdentifier() in ALL_TERMINALS and not self.direct.is_ours(app):
            self._capture(app)


    def _is_skip(self, app):
        """Впереди вкладка Джони (Terminal)."""
        skip = self.skip_tab()
        if not skip or app.bundleIdentifier() not in direct.CAPTURE_TAB:
            return False
        try:
            return direct.capture_tab(app.bundleIdentifier()) == skip
        except Exception:
            return False

    # ---------- запоминание

    def observe_speech_focus(self, app):
        """Compatibility: external applications do not clear terminal input focus."""
        self.speech_focus_lost = False

    def tick(self):
        """Раз в секунду (из ticker): кто впереди. Ушли из терминала — запоминаем его вкладку."""
        app = self.direct.front_app()
        is_term = (app is not None and app.bundleIdentifier() in ALL_TERMINALS
                   and not self.direct.is_ours(app))
        self.observe_speech_focus(app)
        cur = self.current
        if cur is not None and (not is_term or app.processIdentifier() != cur.processIdentifier()):
            self._capture(cur)                       # ушли из терминала: где были
        if is_term and (cur is None or app.processIdentifier() != cur.processIdentifier()) and not self._is_skip(app):
            self._capture(app)                       # сохраняем точную вкладку сразу при входе
        self.current = app if is_term else None

    def note(self, term):
        """Voice сам печатал или переключал вкладку: это тоже последний использованный терминал."""
        if not term:
            return
        if not term.get("via"):                      # вкладка Orca: адрес уже известен
            self._set(self._orca_record(term))
        else:
            app = self.direct.app_by_pid(term.get("pid"), term.get("handle"))
            if app is not None:
                if term.get("bg"):
                    self._set({**self._app_record(app), "kind": "tab", "key": term["bg"]})
                else:
                    self._capture(app)

    def _same_app(self, app):
        rec = self.record
        return bool(rec and rec["pid"] == app.processIdentifier() and rec["bundle"] == app.bundleIdentifier())

    def _set(self, rec):
        with self._lock:
            skip = ("pid", "at")
            if {k: v for k, v in (rec or {}).items() if k not in skip} != {k: v for k, v in (self.record or {}).items() if k not in skip}:
                self.log.debug("Active terminal: %s", {k: v for k, v in (rec or {}).items() if k != "pid"})
            changed = rec != self.record
            self.record = rec
            if changed:
                self._save()

    def _set_app(self, app):
        """Запись «просто приложение» — но точную запись того же приложения не огрубляем."""
        if not self._same_app(app):
            self._set(self._app_record(app))

    @staticmethod
    def _app_record(app):
        return {"kind": "app", "bundle": app.bundleIdentifier(), "pid": app.processIdentifier(),
                "name": app.localizedName() or "?", "at": time.time()}

    @staticmethod
    def _orca_record(term, pid=None):
        return {"kind": "orca", "bundle": direct.ORCA_APP, "pid": pid, "handle": term["handle"],
                "paneKey": term.get("paneKey"), "worktreeId": term.get("worktreeId"),
                "title": term.get("title", ""), "worktree": term.get("worktree", ""), "at": time.time()}

    def _capture(self, app):
        """Точная вкладка приложения, из которого ушли. Не вышло — остаётся запись приложения."""
        bundle = app.bundleIdentifier()
        rec = None
        try:
            if bundle == direct.ORCA_APP:
                term = orca_cli.focused_terminal()
                if term:
                    rec = self._orca_record(term, app.processIdentifier())
            elif bundle in direct.CAPTURE_TAB:
                key = direct.capture_tab(bundle)
                if key and key == self.skip_tab():
                    return                           # ушли из вкладки Джони — прежнюю запись не трогаем
                if key:
                    rec = {**self._app_record(app), "kind": "tab", "key": key}
        except Exception as e:
            self.log.warning("Could not remember active tab %s: %s", bundle, e)
        if rec:
            self._set(rec)
        else:
            self._set_app(app)

    def snapshot(self):
        """Копия записи о последнем терминале (или None)."""
        with self._lock:
            return dict(self.record) if self.record else None

    # ---------- возврат

    def restore(self, force=False):
        """Вернуть фокус в последний терминал и его вкладку.

        force — даже если терминал уже впереди (впереди окно Джони, а вернуться нужно в другой).
        → (цель, статус): цель — как у _focused() (Orca: handle…; напрямую: via…), None — не вернули.
        """
        front = self.direct.front_app()
        if not force and (front is not None and front.bundleIdentifier() in ALL_TERMINALS
                and not self.direct.is_ours(front)):
            return None, t("st.active_here")       # терминал и так впереди — ничего не трогаем

        with self._lock:
            rec = dict(self.record) if self.record else None
        if rec is None:
            return None, t("st.active_none")

        if rec["kind"] == "orca":
            return self._restore_orca(rec)
        app = self.direct.app_by_pid(rec["pid"], rec["bundle"])
        if app is None:
            return None, t("st.active_closed", rec["name"])    # закрыт или перезапущен — другой не берём
        if rec["kind"] == "tab":
            if not direct.restore_tab(rec["bundle"], rec["key"]):
                return None, t("st.active_tab_closed", rec["name"])
            term = self.direct.target(app, "applescript")
        elif rec["bundle"] == direct.ORCA_APP:
            # Orca, но вкладку узнать не удалось: выводим Orca, вкладка — та, что в фокусе в Orca
            term = self.direct.target(app, "keys")
            self.direct.activate(term)
            term = orca_cli.focused_terminal() or term
        else:
            if rec["bundle"] == TERMINAL:
                self._skip_jonny()               # помним только «Терминал», а впереди в нём окно Джони
            route = "applescript" if rec["bundle"] in direct.TERMINALS else "keys"
            term = self.direct.target(app, route)
            self.direct.activate(term)
        return term, None

    def _skip_jonny(self):
        """Впереди в Terminal вкладка Джони — вывести ближайшее другое окно Terminal (если есть)."""
        skip = self.skip_tab()
        if not skip:
            return
        try:
            if direct.capture_tab(TERMINAL) != skip:
                return
            other = direct._osascript(OTHER_TAB % direct._as_string(skip)).strip()
            if other:
                direct.restore_tab(TERMINAL, other)
                self.log.info("Jonny had focus; opening a different window (%s)", other)
        except Exception as e:
            self.log.warning("Active terminal: alternate Terminal window: %s", e)

    def _restore_orca(self, rec):
        name = rec.get("title") or rec.get("worktree") or "Orca"
        if not self.direct.is_running(direct.ORCA_APP):
            return None, t("st.active_closed", "Orca")
        found = orca_cli.find_terminal(rec["handle"], rec.get("paneKey"), rec.get("worktreeId"))
        if found is None:
            return None, t("st.active_tab_closed", name)
        try:
            orca_cli.switch_terminal(found["handle"])
        except RuntimeError as e:
            if "stale" in str(e):                   # закрыли между проверкой и переключением
                return None, t("st.active_tab_closed", name)
            raise
        if found["handle"] != rec["handle"]:
            self.log.info("Active terminal: Orca tab address updated %s → %s", rec["handle"], found["handle"])
            with self._lock:
                if self.record and self.record.get("handle") == rec["handle"]:
                    self.record["handle"] = found["handle"]
                    self._save()
        term = {
            "handle": found["handle"],
            "paneKey": f"{found.get('tabId')}:{found.get('leafId')}",
            "title": found.get("title") or rec.get("title", ""),
            "worktree": rec.get("worktree") or "Orca",
            "worktreeId": found.get("worktreeId"),
            "agent": found.get("agentIdentity"),
            "cwd": found.get("worktreePath"),
            "ptyId": found.get("ptyId"),
            "incarnationId": found.get("incarnationId"),
            "executionHostId": found.get("executionHostId"),
        }
        return term, None
