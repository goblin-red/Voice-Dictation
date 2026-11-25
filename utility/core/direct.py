"""Прямое управление терминалами, без Orca.

Два способа (настройка «target»):
  keys        — нажатия клавиш в окно в фокусе: любой терминал (и вообще любое окно).
                Нужно разрешение macOS «Универсальный доступ».
  applescript — Terminal и iTerm2 через AppleScript: вкладки переключаются командами самого терминала.
                Терминал выходит вперёд; iTerm2 получает текст через AppleScript, Terminal — нажатиями клавиш.
                С «Печатать в фоне» — в последнюю вкладку, терминал вперёд не выходит (см. _bg_text).
"""

import ctypes
import os
import re
import subprocess
import time

import AppKit
import Quartz

from i18n import t

# терминалы с AppleScript: bundle id -> имя приложения
TERMINALS = {
    "com.googlecode.iterm2": "iTerm2",
    "com.apple.Terminal": "Terminal",
}
ITERM = "com.googlecode.iterm2"
ORCA_APP = "com.stablyai.orca"      # Orca: в «умном» способе её вкладки — через Orca CLI
# все известные терминалы: «переключи терминал» выводит терминал вперёд, даже если в фокусе другое приложение
TERMINAL_APPS = (ITERM, "com.apple.Terminal", "com.mitchellh.ghostty", "dev.warp.Warp-Stable", "org.alacritty",
                 "net.kovidgoyal.kitty", "com.github.wez.wezterm", "co.zeit.hyper", "org.tabby")

# коды клавиш macOS
KEY_SPACE = 49
KEY_RETURN = 36
KEY_U = 32          # Ctrl+U — стереть строку ввода
KEY_DELETE = 51     # Backspace — диктовка «Apple» исправляет напечатанное
KEY_RIGHT_BRACKET = 30      # «]»: Cmd+Shift+] — следующая вкладка
KEY_DIGITS = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23}   # Cmd+1…Cmd+5 — вкладка по номеру

CHUNK = 10                  # символов за одно событие (macOS берёт до 20 знаков UTF-16)
DELAY = 0.004               # пауза между событиями, чтобы терминал успевал
ACCESS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
# окна-пульты, в которые диктовку не печатаем (как и в своё окно): macOS видит их как «Python»,
# узнаём по скрипту в команде запуска
HELPER_SCRIPTS = ("goblin_remote.py",)      # окно Goblin Remote


# ---------- разрешение «Универсальный доступ»

try:
    _AX = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    _AX.AXIsProcessTrusted.restype = ctypes.c_bool
except OSError:
    _AX = None


def has_access():
    """Можно ли посылать нажатия клавиш в другие приложения.

    AXIsProcessTrusted видит выданное разрешение сразу, без перезапуска программы.
    """
    try:
        if _AX is not None and _AX.AXIsProcessTrusted():
            return True
        return bool(Quartz.CGPreflightPostEventAccess())
    except Exception:
        return False


def request_access():
    """Системный запрос разрешения + открываем нужную страницу Системных настроек."""
    try:
        Quartz.CGRequestPostEventAccess()
    except Exception:
        pass
    subprocess.Popen(["open", ACCESS_URL])


# ---------- AppleScript

def _osascript(script):
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "osascript")
    return proc.stdout.strip()


def focused_text_input(app):
    """Inspect only the focused control's role/editability; never read its text.

    None means Accessibility could not identify the focused control.
    """
    script = '''tell application "System Events"
        set targetProcess to first application process whose unix id is %d
        tell targetProcess
            set focusedControl to value of attribute "AXFocusedUIElement"
            set controlRole to value of attribute "AXRole" of focusedControl
            if controlRole is in {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"} then return "yes"
            try
                if value of attribute "AXEditable" of focusedControl is true then return "yes"
            end try
            return "no"
        end tell
    end tell'''
    try:
        return _osascript(script % int(app.processIdentifier())).strip() == "yes"
    except Exception:
        return None


def _as_string(text):
    """Строка для AppleScript в кавычках."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Direct:
    """Ввод текста и клавиш в терминал напрямую."""

    def __init__(self, log):
        self.log = log
        self.me = os.getpid()
        self.last_app = None    # последнее приложение в фокусе, кроме окна GOBL(in) Voice и пультов
        self.helpers = {}       # pid -> это окно-пульт (Goblin Remote)?
        self.last_terminal = None   # последний терминал в фокусе — для «переключи терминал» из другого приложения
        self.type_to_previous = True   # фокус на нашем окне: печатать в прошлое приложение (настройка)

    # ---------- кто в фокусе

    @staticmethod
    def _front():
        return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()

    def _ours(self, app):
        """Своё окно или окно-пульт (Goblin Remote): печатать туда нельзя — берём прошлое приложение."""
        pid = app.processIdentifier()
        if pid == self.me:
            return True
        if pid not in self.helpers:
            try:
                cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=2).stdout
            except Exception:
                cmd = ""
            self.helpers[pid] = any(name in cmd for name in HELPER_SCRIPTS)
        return self.helpers[pid]

    def remember_front(self):
        """Раз в секунду: запоминаем приложение в фокусе (кроме нашего окна).

        Нужно для кнопок на пульте: пока нажимаете их, в фокусе окно Voice,
        а клавиши должны уйти в терминал, где вы были до этого.
        """
        app = self._front()
        if app and not self._ours(app):
            self.last_app = app
            if app.bundleIdentifier() in TERMINAL_APPS:
                self.last_terminal = app

    @staticmethod
    def _alive(app):
        return app is not None and not app.isTerminated()

    def _pick(self, route):
        """Приложение, куда отправлять: NSRunningApplication или None."""
        front = self._front()
        if front and self._ours(front):
            if not self.type_to_previous:
                return None     # настройка выключена: фокус не трогаем, не печатаем
            front = None        # фокус на окне Voice или Goblin Remote — берём прошлое приложение

        if route == "keys":
            if front:
                return front
            return self.last_app if self._alive(self.last_app) else None

        # applescript: только Terminal и iTerm2
        for app in (front, self.last_app):
            if self._alive(app) and app.bundleIdentifier() in TERMINALS:
                return app
        running = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
        for bundle in TERMINALS:        # iTerm2 первым
            app = next((a for a in running if a.bundleIdentifier() == bundle), None)
            if app:
                return app
        return None

    def smart_route(self):
        """«Умный» способ: маршрут по приложению в фокусе → (orca | applescript | keys | None, приложение).

        Впереди окно Voice или Goblin Remote — берём прошлое приложение (если это разрешено type_to_previous).
        Orca — вкладки Orca; Terminal и iTerm2 — AppleScript; другие известные терминалы — клавиши;
        постороннее приложение — None: никуда не печатаем и ничего не переключаем.
        """
        app = self._front()
        if app and self._ours(app):
            if not self.type_to_previous:
                return None, None
            app = self.last_app if self._alive(self.last_app) else None
        if app is None:
            return None, None
        bundle = app.bundleIdentifier()
        if bundle == ORCA_APP:
            return "orca", app
        if bundle in TERMINALS:
            return "applescript", app
        if bundle in TERMINAL_APPS:
            return "keys", app
        return None, app

    def target(self, app, route):
        """Цель для ввода в конкретное приложение (выбрано «умным» способом)."""
        return self._target(app, route)

    # ---------- для команды «Активный терминал» (last_terminal.py)

    def front_app(self):
        return self._front()

    def is_ours(self, app):
        return self._ours(app)

    @staticmethod
    def app_by_pid(pid, bundle):
        """Запущенное приложение с этим pid и bundle id — или None (закрыто или перезапущено)."""
        if not pid:
            return None
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None or app.isTerminated() or app.bundleIdentifier() != bundle:
            return None
        return app

    @staticmethod
    def is_running(bundle):
        """Запущено ли приложение с этим bundle id."""
        return any(a.bundleIdentifier() == bundle and not a.isTerminated()
                   for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications())

    def activate(self, term):
        """Вывести приложение цели вперёд (и дождаться фокуса)."""
        self._activate(term)

    def own_front(self):
        """Впереди окно GOBL(in) Voice или пульт (Goblin Remote)."""
        front = self._front()
        return bool(front and self._ours(front))

    def focused(self, route):
        """Цель для ввода: {via, handle, title, worktree, ...} или None."""
        return self._target(self._pick(route), route)

    def _terminal(self, route):
        """Терминал для переключения вкладок: впереди — если это терминал, иначе последний в фокусе,
        иначе любой запущенный (iTerm2 первым). В способе AppleScript — только Terminal и iTerm2."""
        allowed = TERMINALS if route == "applescript" else TERMINAL_APPS
        front = self._front()
        for app in (front, self.last_terminal):
            if self._alive(app) and app.bundleIdentifier() in allowed:
                return self._target(app, route)
        running = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
        for bundle in allowed:
            app = next((a for a in running if a.bundleIdentifier() == bundle), None)
            if app:
                return self._target(app, route)
        return None

    def _target(self, app, route):
        if app is None:
            return None
        return {
            "via": route,
            "handle": app.bundleIdentifier() or "",
            "pid": app.processIdentifier(),
            "title": t("route." + route),
            "worktree": app.localizedName() or "?",
            "paneKey": None,
            "agent": None,
            "cwd": None,
        }

    # ---------- вывод на передний план

    def _activate(self, term):
        """Делает приложение активным (если оно ещё не в фокусе) и ждёт до 1 с."""
        front = self._front()
        if front and front.processIdentifier() == term["pid"]:
            return
        if term["handle"]:
            subprocess.run(["open", "-b", term["handle"]], capture_output=True, timeout=5)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            front = self._front()
            if front and front.processIdentifier() == term["pid"]:
                time.sleep(0.08)    # окно успевает принять фокус клавиатуры
                return
            time.sleep(0.05)
        raise RuntimeError(t("err.no_activate", term["worktree"]))

    # ---------- нажатия клавиш (CGEvent)

    def _check_access(self):
        if not has_access():
            raise RuntimeError(t("err.no_access"))

    @staticmethod
    def _post(event, pid=None):
        """В окно в фокусе — или, если задан pid, только этому процессу (фокус не трогаем)."""
        if pid:
            Quartz.CGEventPostToPid(pid, event)
        else:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _post_key(self, code, flags=0, pid=None):
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
            Quartz.CGEventSetFlags(event, flags)
            self._post(event, pid)
            time.sleep(DELAY)

    def _post_text(self, text, pid=None):
        """Печатает строку как набор символов — раскладка клавиатуры не важна."""
        for i in range(0, len(text), CHUNK):
            piece = text[i:i + CHUNK]
            units = len(piece.encode("utf-16-le")) // 2
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventSetFlags(event, 0)
                Quartz.CGEventKeyboardSetUnicodeString(event, units, piece)
                self._post(event, pid)
            time.sleep(DELAY)

    # ---------- iTerm2 через AppleScript (без фокуса и без «Универсального доступа»)

    @staticmethod
    def _iterm_write(text, newline=False):
        _osascript(
            'tell application "iTerm2" to tell current session of current window to '
            f'write text {_as_string(text)} newline {"YES" if newline else "NO"}'
        )

    @staticmethod
    def _iterm_enter():
        _osascript(
            'tell application "iTerm2" to tell current session of current window to '
            'write text (character id 13) newline NO'
        )

    def _iterm(self, term):
        return term["via"] == "applescript" and term["handle"] == ITERM

    # ---------- «Печатать в фоне»: в запомненную вкладку, терминал не выходит вперёд
    # term["bg"] — tty вкладки Terminal или id сессии iTerm2. activate / open здесь не вызываем.

    def _bg_text(self, term, text):
        if term["handle"] == ITERM:
            self._bg_iterm(term, _as_string(text))
            return
        self._check_access()
        self._bg_select(term)
        self._post_text(text, term["pid"])

    def _bg_key(self, term, key):
        if term["handle"] == ITERM:
            code = {"space": '" "', "clear": "(character id 21)"}.get(key, "(character id 13)")
            self._bg_iterm(term, code)
            return
        self._check_access()
        self._bg_select(term)
        if key == "clear":
            self._post_key(KEY_U, Quartz.kCGEventFlagMaskControl, term["pid"])
        else:
            self._post_key(KEY_SPACE if key == "space" else KEY_RETURN, 0, term["pid"])

    @staticmethod
    def _bg_iterm(term, value):
        """write text в сессию iTerm2 по id, без activate."""
        if _osascript(BG_ITERM_WRITE % (_as_string(term["bg"]), value)).strip() != "ok":
            raise RuntimeError(t("st.active_tab_closed", term["worktree"]))

    @staticmethod
    def _bg_select(term):
        """Terminal: делаем вкладку (tty) выбранной в переднем окне Terminal — без activate.
        Клавиши, посланные процессу Terminal, приходят в его переднее окно."""
        if _osascript(BG_TERMINAL_SELECT % _as_string(term["bg"])).strip() != "ok":
            raise RuntimeError(t("st.active_tab_closed", term["worktree"]))

    # ---------- команды

    def send_text(self, term, text, *, preserve_whitespace=False):
        if not preserve_whitespace:
            tail = " " if text[-1:].isspace() else ""
            text = " ".join(text.split()) + tail  # без переводов строк — иначе агент отправит раньше времени
        if not text:
            return
        if term.get("bg"):
            self._bg_text(term, text)
            return
        if self._iterm(term):
            self._activate(term)        # без «Печатать в фоне» терминал выходит вперёд, как Terminal
            self._iterm_write(text)
            return
        self._check_access()
        self._activate(term)
        self._post_text(text)

    def _erase_burst(self, n, pid=None):
        """Backspace пачками по 20 без пауз между нажатиями: 1000 знаков — ~0,6 с вместо ~8 с, строка исчезает
        разом. Сплошной поток без передышек Terminal теряет (из 1000 доходило 762) — пауза 10 мс после каждой пачки."""
        for i in range(n):
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, KEY_DELETE, down)
                Quartz.CGEventSetFlags(event, 0)
                self._post(event, pid)
            if i % 20 == 19:
                time.sleep(0.01)
        time.sleep(DELAY)

    def edit(self, term, erase, text, burst=False):
        """Диктовка «Apple»: стереть erase символов и допечатать text как есть (поправка распознавания).
        burst — стереть разом («удали фразу», «Стирать сразу»), а не по одному знаку."""
        if not erase and not text:
            return
        if term["handle"] == ITERM:
            # iTerm2: Backspace — символ 127, всё одной записью
            value = " & ".join(["(character id 127)"] * erase + ([_as_string(text)] if text else []))
            if term.get("bg"):
                self._bg_iterm(term, value)
            else:
                self._activate(term)
                _osascript('tell application "iTerm2" to tell current session of current window to '
                           f'write text ({value}) newline NO')
            return
        self._check_access()
        pid = None
        if term.get("bg"):
            self._bg_select(term)
            pid = term["pid"]
        else:
            self._activate(term)
        if burst:
            self._erase_burst(erase, pid)
        else:
            for _ in range(erase):
                self._post_key(KEY_DELETE, 0, pid)
        if text:
            self._post_text(text, pid)

    def send_key(self, term, key):
        if term.get("bg"):
            self._bg_key(term, key)
            return
        if self._iterm(term):
            self._activate(term)
            if key == "space":
                self._iterm_write(" ")
            elif key == "clear":
                _osascript('tell application "iTerm2" to tell current session of current window to '
                           'write text (character id 21) newline NO')
            else:
                self._iterm_enter()
            return
        self._check_access()
        self._activate(term)
        if key == "clear":
            self._post_key(KEY_U, Quartz.kCGEventFlagMaskControl)
            return
        self._post_key(KEY_SPACE if key == "space" else KEY_RETURN)

    def switch_to(self, route, n, term=None):
        """Вкладка №n в переднем окне терминала (или в term). Цель или None, если такой вкладки нет."""
        term = term or self._terminal(route)
        if term is None:
            return None
        self._activate(term)                  # терминал — вперёд, даже если в фокусе было другое приложение
        if term["handle"] in TERMINALS:
            # номер — по всем окнам и вкладкам, окна в порядке открытия (id растёт): у кого-то вкладки,
            # у кого-то отдельные окна — «первый терминал» одинаково значит самый первый открытый
            sessions = []
            for line in _osascript(LIST_SESSIONS[term["handle"]]).splitlines():
                wid, _, count = line.partition(":")
                if wid.strip().isdigit() and count.strip().isdigit():
                    sessions.append((int(wid), int(count)))
            flat = [(wid, tab) for wid, count in sorted(sessions) for tab in range(1, count + 1)]
            if not 1 <= n <= len(flat):
                return None
            _osascript(SELECT_SESSION[term["handle"]] % flat[n - 1])
            return {**term, "switched": "tab"}
        # другой терминал: стандартное Cmd+N
        self._check_access()
        self._post_key(KEY_DIGITS[n], Quartz.kCGEventFlagMaskCommand)
        return {**term, "switched": "tab"}

    def select_agent_session(self, side, route):
        bundle = side.get("terminal_app") or "com.apple.Terminal"
        tty = side["tty"]
        if bundle == ITERM:
            script = '''tell application "iTerm2"
repeat with w in windows
repeat with tb in tabs of w
repeat with ss in sessions of tb
if tty of ss is %s then
select ss
select tb
select w
activate
return "ok"
end if
end repeat
end repeat
end repeat
end tell
return "missing"''' % _as_string(tty)
            if _osascript(script).strip() != "ok":
                raise RuntimeError("The iTerm2 session closed")
        elif bundle == "com.apple.Terminal":
            self._bg_select({"bg": tty, "worktree": tty})
        else:
            raise RuntimeError("Unknown terminal app: " + bundle)
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
        if not apps:
            raise RuntimeError("The terminal app closed")
        app = apps[0]
        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        return {**self.target(app, route), "title": side.get("nick", tty), "agent": side.get("agent")}

    def switch(self, route, term=None):
        """Следующая вкладка терминала, а если вкладка в окне одна — следующее окно.

        Возвращает цель (с полем "switched": tab / window) или None, если переключать некуда.
        Terminal и iTerm2 переключаются их собственными командами (AppleScript) в обоих способах;
        другие терминалы в способе «клавиши» — сочетанием Cmd+Shift+].
        term — конкретный терминал («умный» способ), иначе выбираем сами.
        """
        term = term or self._terminal(route)
        if term is None:
            return None
        self._activate(term)                  # терминал — вперёд, даже если в фокусе было другое приложение

        if term["handle"] in TERMINALS:
            result = _osascript(ITERM_SWITCH if term["handle"] == ITERM else TERMINAL_SWITCH).strip()
            if result not in ("tab", "window"):
                return None
            return {**term, "switched": result}

        # другой терминал (Ghostty, Warp…): стандартное «следующая вкладка»
        self._check_access()
        flags = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
        self._post_key(KEY_RIGHT_BRACKET, flags)
        return {**term, "switched": "tab"}


# ---------- AppleScript: терминал по номеру среди всех окон и вкладок

# «id окна:число вкладок» по строке на окно
LIST_SESSIONS = {
    "com.apple.Terminal": '''
tell application "Terminal"
  set out to ""
  repeat with w in windows
    set out to out & (id of w) & ":" & (count of tabs of w) & linefeed
  end repeat
  return out
end tell''',
    ITERM: '''
tell application "iTerm2"
  set out to ""
  repeat with w in windows
    set out to out & (id of w) & ":" & (count of tabs of w) & linefeed
  end repeat
  return out
end tell''',
}

# вывести вперёд окно (id) и выбрать в нём вкладку (номер)
SELECT_SESSION = {
    "com.apple.Terminal": '''
tell application "Terminal"
  set w to window id %d
  set selected tab of w to tab %d of w
  set index of w to 1
  activate
end tell''',
    ITERM: '''
tell application "iTerm2"
  set w to window id %d
  tell w to select
  tell w to select tab %d
  activate
end tell''',
}


# ---------- AppleScript переключения: следующая вкладка, иначе следующее окно (по кругу)

TERMINAL_SWITCH = '''
tell application "Terminal"
  set w to front window
  set n to count of tabs of w
  if n > 1 then
    repeat with i from 1 to n
      if selected of tab i of w then
        set selected tab of w to tab ((i mod n) + 1) of w
        return "tab"
      end if
    end repeat
  end if
  set wc to count of windows
  if wc < 2 then return "none"
  set index of window wc to 1
  return "window"
end tell'''

ITERM_SWITCH = '''
tell application "iTerm2"
  tell current window
    set n to count of tabs
    if n > 1 then
      set cur to id of current session
      repeat with i from 1 to n
        if id of current session of tab i is cur then
          select tab ((i mod n) + 1)
          return "tab"
        end if
      end repeat
    end if
  end tell
  set wc to count of windows
  if wc < 2 then return "none"
  tell window wc to select
  return "window"
end tell'''


# ---------- AppleScript для «Активного терминала»: где вкладка сейчас и как в неё вернуться
# Terminal — по tty вкладки, iTerm2 — по id сессии (панели): оба не меняются, пока вкладка открыта

# tty вкладки впереди — по нему находим папки процессов вкладки (озвучка «только активное окно»)
FRONT_TTY = {
    "com.apple.Terminal": 'tell application "Terminal" to return tty of selected tab of front window',
    ITERM: 'tell application "iTerm2" to return tty of current session of current window',
}


def front_tab_dirs(bundle):
    """Рабочие папки процессов во вкладке впереди (Terminal / iTerm2). None — не удалось узнать."""
    script = FRONT_TTY.get(bundle)
    if not script:
        return None
    try:
        tty = _osascript(script).strip()
        if not tty.startswith("/dev/"):
            return None
        return _tty_dirs(tty)
    except Exception:
        return None


# имя вкладки впереди — строка «Активный терминал» на Главной Voice
FRONT_NAME = {
    "com.apple.Terminal": 'tell application "Terminal" to return name of front window',
    ITERM: 'tell application "iTerm2" to return name of current session of current window',
}


def front_tab_name(bundle):
    """Заголовок вкладки впереди (Terminal / iTerm2) или "" — не удалось узнать."""
    script = FRONT_NAME.get(bundle)
    if not script:
        return ""
    try:
        name = _osascript(script).strip()
    except Exception:
        return ""
    # Terminal: «папка — заголовок — команда — ttys001 — 107×30»: нужны только папка и заголовок
    return " — ".join(name.split(" — ")[:2]) if bundle == "com.apple.Terminal" else name


def tab_dirs(term):
    """Рабочие папки процессов во вкладке Terminal / iTerm2, куда печатаем («агент работает»: чей ответ ждём).
    None — не удалось узнать."""
    bundle = term.get("handle")
    if bundle not in FRONT_TTY:
        return None
    try:
        tty = term.get("tty") or term.get("bg") or ""
        if not tty.startswith("/dev/"):
            tty = _osascript(FRONT_TTY[bundle]).strip()
        if not tty.startswith("/dev/"):
            return None
        return _tty_dirs(tty)
    except Exception:
        return None


def _tty_dirs(tty):
    pids = subprocess.run(["ps", "-t", tty[5:], "-o", "pid="], capture_output=True, text=True, timeout=2).stdout.split()
    if not pids:
        return set()
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", ",".join(pids), "-Fn"],
                         capture_output=True, text=True, timeout=3).stdout
    return {line[1:] for line in out.splitlines() if line.startswith("n/")}


AGENTS = ("claude", "codex", "opencode", "grok")   # имена CLI-агентов среди процессов вкладки


def tab_agent(term):
    """Какой агент запущен во вкладке Terminal / iTerm2, куда печатаем (для журнала). None — не узнали."""
    bundle = term.get("handle")
    if bundle not in FRONT_TTY:
        return None
    try:
        tty = term.get("bg") if str(term.get("bg") or "").startswith("/dev/") else _osascript(FRONT_TTY[bundle]).strip()
        if not tty.startswith("/dev/"):
            return None
        out = subprocess.run(["ps", "-t", tty[5:], "-o", "command="], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    words = set(re.split(r"[\s/]+", out.lower()))
    return next((a for a in AGENTS if a in words), None)


CAPTURE_TAB = {
    "com.apple.Terminal": 'tell application "Terminal" to return tty of selected tab of front window',
    ITERM: 'tell application "iTerm2" to return id of current session of current window',
}

RESTORE_TAB = {
    "com.apple.Terminal": '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        set selected tab of w to t
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell''',
    ITERM: '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is %s then
          tell w to select
          tell t to select
          tell s to select
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
  return "none"
end tell''',
}


def capture_tab(bundle):
    """Вкладка Terminal / iTerm2 в переднем окне: tty или id сессии. None — не удалось узнать."""
    key = _osascript(CAPTURE_TAB[bundle]).strip()
    return key or None


def restore_tab(bundle, key):
    """Выбрать запомненную вкладку и вывести терминал вперёд. False — такой вкладки больше нет.
    Вызывать только для запущенного терминала: AppleScript запустил бы закрытый."""
    return _osascript(RESTORE_TAB[bundle] % _as_string(key)).strip() == "ok"



# ---------- AppleScript для «Печатать в фоне» (без activate)

# Terminal: выбрать вкладку по tty и сделать её окно передним внутри Terminal (приложение вперёд не выходит)
BG_TERMINAL_SELECT = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %s then
        set selected tab of w to t
        if index of w is not 1 then set index of w to 1
        return "ok"
      end if
    end repeat
  end repeat
  return "none"
end tell'''

# iTerm2: write text в сессию по id (newline NO — Enter отдельно)
BG_ITERM_WRITE = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is %s then
          tell s to write text %s newline NO
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
  return "none"
end tell'''
