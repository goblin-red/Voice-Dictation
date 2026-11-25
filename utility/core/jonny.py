"""Свой терминал агента («Джони»): вкладка Terminal, привязанная к Voice.

Voice при старте ищет вкладку и открывает её, только если её ещё нет.
Вкладку узнаём по заголовку и по процессу: привязанная вкладка, в которой работает агент (claude) из папки Джони,
остаётся привязанной, даже если её заголовок сменился (заголовок тогда возвращаем).
Печатаем только туда, где агент жив: вкладка с голой оболочкой (claude вышел) — не цель,
в ней Джони запускается заново (речь не выполняется как команды оболочки).
Печать — в фоне по tty (direct.py).
"""

import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import AppKit

import direct

TERMINAL = "com.apple.Terminal"

# все вкладки Terminal: «tty<TAB>заголовок» по строке (слово tab внутри Terminal — вкладка, поэтому character id 9)
LIST_TABS = '''
tell application "Terminal"
  set out to ""
  repeat with w in windows
    repeat with t in tabs of w
      set out to out & (tty of t) & (character id 9) & (custom title of t) & linefeed
    end repeat
  end repeat
  return out
end tell'''

# новая вкладка (окно) с командой; заголовок — имя
LAUNCH = '''
tell application "Terminal"
  set t to do script %s
  set custom title of t to %s
  return tty of t
end tell'''

# та же вкладка (в ней осталась только оболочка): команда запуска — в неё, заголовок — имя
REVIVE = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        do script %s in t
        set custom title of t to %s
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''

# вернуть заголовок вкладке (агент в ней жив, а заголовок сменили)
SET_TITLE = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        set custom title of t to %s
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''

# мёртвая вкладка «Джони» (claude вышел по exec — «[Процесс завершён]», процессов нет): закрыть её окно,
# если вкладка в нём одна — иначе после нового запуска старое окно с тем же заголовком так и висело бы
CLOSE_DEAD = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s and (count of processes of t) = 0 and (count of tabs of w) = 1 then
        close w
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''

# оформление вкладки: профиль Terminal по имени («Homebrew», «Ocean»…)
SET_PROFILE = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        set current settings of t to settings set %s
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''

# профили Terminal — для выбора в окне
LIST_PROFILES = 'tell application "Terminal" to get name of every settings set'

SHELLS = {"zsh", "bash", "sh", "fish", "login", "tcsh", "csh", "ksh", "dash"}
RUNTIMES = {"node", "bun", "deno"}     # агент может быть скриптом: «node …/claude-code/cli.js»
LAUNCH_GRACE_S = 20.0    # столько секунд после запуска вкладка считается «запускается»: второй раз не открываем
TITLE_FIX_S = 30.0       # заголовок возвращаем не чаще (агент может снова его менять)
STATUS_CHECK_S = 10.0    # для окна: привязанную вкладку перепроверяем не чаще
APP_RETRY_S = 0.2        # AppKit не вернул Terminal — повтор через столько


class _PidApp:
    """Terminal запущен (его видит pgrep), а AppKit из фонового потока его не вернул.
    Заменяет NSRunningApplication там, где Voice нужен только pid и вывод вперёд."""

    def __init__(self, pid):
        self.pid = pid

    def processIdentifier(self):
        return self.pid

    @staticmethod
    def bundleIdentifier():
        return TERMINAL

    @staticmethod
    def localizedName():
        return "Terminal"

    def isTerminated(self):
        try:
            os.kill(self.pid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    @staticmethod
    def activateWithOptions_(_options):
        direct._osascript('tell application id "com.apple.Terminal" to activate')
        return True


class Jonny:
    def __init__(self, direct_, log):
        self.direct = direct_
        self.log = log
        self.cfg = {}
        self.tty = None          # привязанная вкладка: /dev/ttysNNN
        self.launched_at = 0.0
        self._launched_tty = None
        self._title_fixed = {}   # tty -> когда возвращали заголовок
        self._dupes = ()         # о каких двойниках уже предупредили
        self._checked = 0.0      # когда status() перепроверял вкладку
        self._lock = threading.RLock()

    # ---------- настройки

    def name(self):
        return (self.cfg.get("name") or "Jonny").strip()

    def command(self):
        return (self.cfg.get("command") or "").strip()

    def enabled(self):
        return bool(self.cfg.get("enabled", True)) and bool(self.command())

    def cwd(self):
        """Папка агента из cd или параметра Codex -C / --cd."""
        try:
            args = shlex.split(self.command())
        except ValueError:
            args = self.command().split()
        for i, arg in enumerate(args):
            if arg in ("-C", "--cd") and i + 1 < len(args):
                return str(Path(args[i + 1]).expanduser().resolve())
            if arg.startswith("--cd="):
                return str(Path(arg.split("=", 1)[1]).expanduser().resolve())
        m = re.search(r"\bcd\s+(['\"]?)(.+?)\1(?:\s*(?:&&|;)|$)", self.command())
        return os.path.expanduser(m.group(2)).rstrip("/") if m else None

    def agent(self):
        """Имя агента из команды запуска (claude / codex / …): его процесс должен жить во вкладке."""
        try:
            words = shlex.split(self.command())
        except ValueError:
            words = self.command().split()
        for word in words:
            if os.path.basename(word) in direct.AGENTS:
                return os.path.basename(word)
        return "claude"

    def voice_for(self, ev):
        """Ответ Джони (Claude из его папки) — свой голос озвучки; чужой — None."""
        cwd = self.cwd()
        if not self.enabled() or not cwd or (ev.get("cwd") or "").rstrip("/") != cwd:
            return None
        voices = {k: self.cfg.get(k + "_voice") for k in ("silero", "grok")}
        return {k: v for k, v in voices.items() if v} or None

    # ---------- Terminal запущен?

    @staticmethod
    def _native_app():
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TERMINAL)
        return next((a for a in apps if not a.isTerminated()), None)

    @staticmethod
    def _terminal_pid():
        """pid Terminal по pgrep или None — запасная проверка, когда AppKit вернул пусто."""
        try:
            out = subprocess.run(["pgrep", "-x", "Terminal"], capture_output=True, text=True, timeout=2).stdout.split()
            return int(out[0]) if out else None
        except Exception:
            return None

    @classmethod
    def _terminal_app(cls):
        """Запущенный Terminal или None. AppKit из фонового потока иногда возвращает пусто, хотя Terminal работает
        (ложное «Terminal не запущен» — фраза терялась, открывался второй терминал): тогда спрашиваем pgrep
        и повторяем через APP_RETRY_S; pgrep видит Terminal, а AppKit всё нет — замена по pid."""
        pid = None
        for attempt in range(2):
            app = cls._native_app()
            if app is not None:
                return app
            pid = cls._terminal_pid()
            if pid is None:
                return None                      # и pgrep не видит — Terminal правда не запущен
            native = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if native is not None and not native.isTerminated():
                return native
            if attempt == 0:
                time.sleep(APP_RETRY_S)
        return _PidApp(pid)

    # ---------- вкладки и их процессы

    @staticmethod
    def _tabs():
        """[(tty, заголовок)] всех вкладок Terminal."""
        out = []
        for line in direct._osascript(LIST_TABS).splitlines():
            tty, _, title = line.partition("\t")
            if tty.startswith("/dev/"):
                out.append((tty.strip(), title.strip()))
        return out

    @staticmethod
    def _procs(tty):
        """Процессы вкладки: [(pid, на переднем плане, имя программы, первый аргумент)]."""
        out = subprocess.run(["ps", "-t", tty.replace("/dev/", "", 1), "-o", "pid=,stat=,args="],
                             capture_output=True, text=True, timeout=2).stdout
        procs = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            argv = parts[2].split()
            procs.append((int(parts[0]), "+" in parts[1], os.path.basename(argv[0]).lstrip("-") if argv else "",
                          argv[1] if len(argv) > 1 else ""))
        return procs

    @staticmethod
    def _cwd_of(pid):
        """Рабочая папка процесса (lsof) или None."""
        try:
            out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                                 capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
        return next((line[1:] for line in out.splitlines() if line.startswith("n/")), None)

    def _is_agent(self, proc):
        _, _, name, arg1 = proc
        agent = self.agent()
        if name == agent:
            return True
        return name in RUNTIMES and bool(re.search(r"(^|/)%s([-./]|$)" % re.escape(agent), arg1))

    def _state(self, tty):
        """Кто во вкладке: agent — агент жив; shell — только оболочка (агент вышел); busy — другая программа;
        dead — процессов нет (вкладка «завершена»); None — не удалось узнать."""
        try:
            procs = self._procs(tty)
        except Exception as e:
            self.log.debug("%s: processes in %s: %s", self.name(), tty, e)
            return None
        if not procs:
            return "dead"
        if any(self._is_agent(p) for p in procs):
            return "agent"
        front = [p for p in procs if p[1]]
        return "shell" if all(p[2] in SHELLS for p in front) else "busy"

    def _in_folder(self, tty):
        """Агент вкладки работает в папке Джони (папки нет в команде — любой агент годится)."""
        cwd = self.cwd()
        if not cwd:
            return True
        want = os.path.realpath(cwd)
        try:
            agents = [p[0] for p in self._procs(tty) if self._is_agent(p)]
        except Exception:
            return False
        return any(os.path.realpath(self._cwd_of(pid) or "") == want for pid in agents)

    def _starting(self, tty=None):
        """Вкладку только что открыли (агент ещё грузится)."""
        if time.time() - self.launched_at >= LAUNCH_GRACE_S:
            return False
        return tty is None or tty == self._launched_tty

    # ---------- поиск вкладки

    def find(self):
        """tty вкладки Джони (агент в ней жив) или None. Сначала проверяем уже привязанную."""
        with self._lock:
            return self._find()

    def _missing_diagnostic(self, stage):
        try:
            pids = subprocess.run(["pgrep", "-x", "Terminal"], capture_output=True,
                                  text=True, timeout=2).stdout.split()
            apps = [(a.processIdentifier(), bool(a.isTerminated())) for a in
                    AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TERMINAL)]
            self.log.warning("jonny.lookup: missing=%s bound=%r os_pids=%r native=%r",
                             stage, self.tty, pids, apps)
        except Exception as e:
            self.log.warning("jonny.lookup: missing=%s diagnostic_error=%s", stage, e)

    def _find(self, app=None):
        app = app if app is not None else self._terminal_app()
        if app is None:
            self._missing_diagnostic("application_before_tabs")
            self.tty = None
            return None
        return self._lookup(app)["tty"]

    def _lookup(self, app):
        """{"tty": живая вкладка Джони или None, "idle": вкладка «Джони» с одной оболочкой или None,
        "dead": вкладки «Джони» без процессов (claude вышел) — их закрывают перед новым запуском}.

        Порядок: привязанная вкладка (с любым заголовком, если агент в ней — из папки Джони), вкладки с заголовком,
        затем любая вкладка, где агент работает в папке Джони (заголовок сменили, а Voice перезапускали)."""
        tabs = self._tabs()
        ttys = [tty for tty, _ in tabs]
        titles = dict(tabs)
        named = [tty for tty, title in tabs if title == self.name()]
        order = ([self.tty] if self.tty in ttys else []) + [tty for tty in named if tty != self.tty]
        live, idle, states = [], None, {}
        for tty in order:
            state = "starting" if self._starting(tty) else self._state(tty)
            states[tty] = state
            if state in ("agent", "starting") or (state is None and tty in named):
                if tty in named or state == "starting" or self._in_folder(tty):
                    live.append(tty)
            elif state == "shell" and tty in named and idle is None:
                idle = tty
        if not live and self.cwd():
            for tty in ttys:
                if tty not in order and self._state(tty) == "agent" and self._in_folder(tty):
                    live.append(tty)
                    break
        self.log.debug("jonny.lookup: pid=%s bound=%r expected=%r tabs=%r states=%r live=%r idle=%r",
                       app.processIdentifier(), self.tty, self.name(), tabs, states, live, idle)
        if not named and not live:
            self._missing_diagnostic("named_tab")
        alive = [tty for tty in live if states.get(tty) in ("agent", None)]
        if len(alive) > 1 and tuple(alive) != self._dupes:
            self.log.warning("%s: several tabs contain the agent (%s); typing in %s",
                             self.name(), ", ".join(alive), live[0])
        self._dupes = tuple(alive) if len(alive) > 1 else ()
        tty = live[0] if live else None
        if tty and tty != self.tty:
            self.log.info("%s: bound to tab %s", self.name(), tty)
        if tty and titles.get(tty) != self.name():
            self._fix_title(tty, titles.get(tty))
        if idle and not tty:
            self.log.warning("%s: tab %s only has a shell and does not contain %s",
                             self.name(), idle, self.agent())
        self.tty = tty
        return {"tty": tty, "idle": idle, "dead": [t for t in named if states.get(t) == "dead"]}

    def _fix_title(self, tty, old):
        """Агент жив, а заголовок вкладки другой («✳ Claude Code», printf, ssh) — вернуть имя."""
        now = time.time()
        if now - self._title_fixed.get(tty, 0) < TITLE_FIX_S:
            return
        self._title_fixed[tty] = now
        try:
            direct._osascript(SET_TITLE % (direct._as_string(tty), direct._as_string(self.name())))
            self.log.info("%s: restored tab %s title from “%s” to “%s”", self.name(), tty, old or "", self.name())
        except Exception as e:
            self.log.warning("%s: tab title: %s", self.name(), e)

    # ---------- запуск

    def launch(self):
        """Открывает вкладку с командой. tty или None."""
        tty = direct._osascript(LAUNCH % (direct._as_string(self.command()), direct._as_string(self.name()))).strip()
        self.launched_at = time.time()
        self._launched_tty = tty if tty.startswith("/dev/") else None
        self.tty = self._launched_tty
        self.log.info("%s: opened terminal (%s)", self.name(), self.tty or "?")
        return self.tty

    def _relaunch(self, dead):
        """Новый запуск; мёртвые вкладки «Джони» (окно из одной вкладки) прежде закрыть."""
        for tty in dead:
            try:
                if direct._osascript(CLOSE_DEAD % direct._as_string(tty)).strip() == "ok":
                    self.log.info("%s: closed window %s after the agent exited", self.name(), tty)
            except Exception as e:
                self.log.warning("%s: inactive window %s: %s", self.name(), tty, e)
        return self.launch()

    def revive(self, tty):
        """Во вкладке «Джони» осталась оболочка (агент вышел) — запустить агента в ней же. tty или None."""
        ok = direct._osascript(REVIVE % (direct._as_string(tty), direct._as_string(self.command()),
                                         direct._as_string(self.name()))).strip() == "ok"
        if not ok:
            return None
        self.launched_at = time.time()
        self._launched_tty = self.tty = tty
        self.log.info("%s: restarted the agent in tab %s", self.name(), tty)
        return tty

    def ensure(self):
        """При старте Voice: терминал уже есть — привязываем, нет — открываем. Ставим его оформление."""
        with self._lock:
            return self._ensure()

    def _ensure(self):
        if not self.enabled():
            return None
        try:
            app = self._terminal_app()
            found = self._lookup(app) if app is not None else {"tty": None, "idle": None, "dead": []}
            tty = found["tty"]
            if tty:
                self.log.info("%s: bound to the open terminal (%s)", self.name(), tty)
            elif self._starting() and self._launched_tty is None:
                self.log.info("%s: waiting for the new terminal to start", self.name())
                return None
            elif found["idle"]:
                tty = self.revive(found["idle"]) or self._relaunch(found["dead"])
            else:
                tty = self._relaunch(found["dead"])
            self.apply_profile()
            return tty
        except Exception as e:
            self.log.warning("%s: %s", self.name(), e)
            return None

    def apply_profile(self):
        """Своё оформление вкладки Джони (профиль Terminal); пусто — не трогаем."""
        profile = (self.cfg.get("profile") or "").strip()
        if not profile or not self.tty:
            return
        try:
            direct._osascript(SET_PROFILE % (direct._as_string(self.tty), direct._as_string(profile)))
        except Exception as e:
            self.log.warning("%s: appearance “%s”: %s", self.name(), profile, e)

    @staticmethod
    def profiles():
        """Профили Terminal (названия) — для выбора в окне."""
        try:
            return [p.strip() for p in direct._osascript(LIST_PROFILES).split(",") if p.strip()]
        except Exception:
            return []

    # ---------- цель для печати

    def target(self, allow_restart=True):
        """Цель для печати в фоне (как у direct) или None — вкладки нет или в ней нет агента
        (тогда агент перезапускается в той же вкладке в фоне, а фразу надо повторить)."""
        with self._lock:
            app = self._terminal_app()
            if app is None:
                self._missing_diagnostic("application_before_target")
                return None
            found = self._lookup(app)
            tty = found["tty"]
            if not tty:
                if allow_restart and found["idle"] and self.enabled() and not self._starting():
                    threading.Thread(target=self.ensure, daemon=True).start()   # агент вышел — запустить заново
                return None
            if app.isTerminated():
                self._missing_diagnostic("application_terminated")
                return None
            return {**self.direct.target(app, "applescript"), "bg": tty, "jonny": True, "worktree": self.name()}

    @staticmethod
    def front():
        """Что было впереди до «Джони»: {app, tty} — вернуть после Enter."""
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        tty = None
        if app is not None and app.bundleIdentifier() == TERMINAL:
            try:
                tty = direct.capture_tab(TERMINAL)
            except Exception:
                tty = None
        return {"app": app, "tty": tty}

    def show(self):
        """Окно Джони — вперёд: его вкладка выбрана, Terminal активен."""
        if self.tty:
            direct._osascript(direct.BG_TERMINAL_SELECT % direct._as_string(self.tty))
        app = self._terminal_app()
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    def restore(self, back):
        """После Enter: прежняя вкладка Terminal и прежнее приложение — снова впереди."""
        if not back:
            return
        try:
            if back.get("tty") and back["tty"] != self.tty:
                direct._osascript(direct.BG_TERMINAL_SELECT % direct._as_string(back["tty"]))
            app = back.get("app")
            if app is not None and not app.isTerminated():
                app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        except Exception as e:
            self.log.warning("%s: restoring window: %s", self.name(), e)

    # ---------- состояние для окна

    def status(self):
        """Для окна: {on, name, tty}. Привязанную вкладку перепроверяем в фоне (не чаще STATUS_CHECK_S):
        закрытая вкладка не висит в состоянии до следующего «Джони терминал»."""
        if self.tty and time.time() - self._checked >= STATUS_CHECK_S:
            self._checked = time.time()
            threading.Thread(target=self.refresh, daemon=True).start()
        return {"on": self.enabled(), "name": self.name(), "tty": self.tty}

    def refresh(self):
        """Привязанной вкладки больше нет (или Terminal закрыт) — забыть её. Ничего не открывает и не печатает."""
        if not self._lock.acquire(blocking=False):
            return                               # идёт поиск или запуск — он и обновит
        try:
            if not self.tty:
                return
            app = self._terminal_app()
            if app is None:
                self.log.info("%s: Terminal closed; tab %s unbound", self.name(), self.tty)
                self.tty = None
                return
            if self.tty not in [tty for tty, _ in self._tabs()]:
                self.log.info("%s: tab %s closed and unbound", self.name(), self.tty)
                self.tty = None
        except Exception as e:
            self.log.debug("%s: tab check: %s", self.name(), e)
        finally:
            self._lock.release()
