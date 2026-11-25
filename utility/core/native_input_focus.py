"""Identify an exact Terminal tab or iTerm2 session for fixed Voice input."""

import hashlib
import os
import subprocess

import AppKit

from direct import ITERM, TERMINALS, _osascript
from orchestration import ITERM_TABS_SCRIPT, TABS_SCRIPT, TERMINAL


APPS = {TERMINAL: TABS_SCRIPT, ITERM: ITERM_TABS_SCRIPT}


def _processes(pids=None):
    fields = "pid=,ppid=,tty=,lstart="
    args = ["-axo", fields] if pids is None else ["-p", ",".join(map(str, pids)), "-o", fields]
    try:
        result = subprocess.run(
            ["ps", *args], env={**os.environ, "LC_ALL": "C"},
            capture_output=True, text=True, timeout=3, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    processes = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        pid, parent, tty, started = parts
        if pid.isdigit() and parent.isdigit():
            processes[int(pid)] = (int(parent), tty, " ".join(started.split()))
    return processes


def _tab_rows(bundle, app):
    # AppleScript would start a closed app, so check the instance first.
    if app.isTerminated():
        return []
    try:
        output = _osascript(APPS[bundle])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []
    rows = []
    expected = 4 if bundle == ITERM else 3
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == expected and fields[0].startswith("/dev/") and fields[1].isdigit() and fields[2].isdigit():
            tty, window, tab = fields[:3]
            key = fields[3] if bundle == ITERM else tty
            if key:
                rows.append((tty, window, tab, key))
    return rows


def _session_anchor(processes, tty):
    # The root attached to the PTY survives foreground agent changes.
    # Its PID and birth distinguish the tab from a new tab reusing the same TTY.
    name = tty.removeprefix("/dev/")
    roots = [
        (pid, started) for pid, (parent, device, started) in processes.items()
        if device == name and processes.get(parent, (None, None, None))[1] != name
    ]
    # The login process is normally the only root for a Terminal tab.
    return roots[0] if len(roots) == 1 else None


def list_input_terminals(direct_):
    """List addressed native tabs, including shells, without switching focus."""
    processes = _processes()
    rows = []
    for bundle in APPS:
        apps = [app for app in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
                if not app.isTerminated()]
        if len(apps) != 1:
            continue
        app = apps[0]
        pid = int(app.processIdentifier())
        if pid not in processes or not direct_.app_by_pid(pid, bundle):
            continue
        birth = processes[pid][2]
        windows = {}
        for tty, window, tab, key in _tab_rows(bundle, app):
            anchor = _session_anchor(processes, tty) if bundle == TERMINAL else None
            if bundle == TERMINAL and not anchor:
                continue
            window_number = windows.setdefault(window, len(windows) + 1)
            identity = (bundle, pid, birth, key, anchor)
            identifier = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
            rows.append({
                "id": "native:" + identifier,
                "label": f"{TERMINALS[bundle]} · {window_number}/{tab} · {tty.removeprefix('/dev/')}",
                "bundle": bundle, "pid": pid, "birth": birth, "bg": key, "tty": tty,
                "anchor_pid": anchor[0] if anchor else None,
                "anchor_birth": anchor[1] if anchor else None,
            })
    return rows


def resolve_input_terminal(direct_, target):
    """Return the exact foreground-independent target, or None if it expired."""
    if not isinstance(target, dict):
        return None
    bundle, pid, key = target.get("bundle"), target.get("pid"), target.get("bg")
    if not isinstance(bundle, str) or bundle not in APPS or type(pid) is not int or pid <= 1 or not isinstance(key, str):
        return None
    app = direct_.app_by_pid(pid, bundle)
    if app is None:
        return None
    anchor = target.get("anchor_pid")
    if bundle == TERMINAL and (type(anchor) is not int or anchor <= 1 or not key.startswith("/dev/")):
        return None
    processes = _processes([pid, anchor] if bundle == TERMINAL else [pid])
    if pid not in processes or processes[pid][2] != target.get("birth"):
        return None
    if bundle == TERMINAL and (
        anchor not in processes or processes[anchor][1:] != (key.removeprefix("/dev/"), target.get("anchor_birth"))
    ):
        return None
    tabs = [tty for tty, _, _, native_key in _tab_rows(bundle, app) if native_key == key]
    if len(tabs) != 1:
        return None
    label = target.get("label") or TERMINALS[bundle]
    return {**direct_.target(app, "applescript"), "bg": key, "tty": tabs[0],
            "title": label, "worktree": label}
