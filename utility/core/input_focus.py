"""Persistent, explicit destination for text and keys sent by Voice."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

import native_input_focus
import orca_cli
import orchestration
from paths import STATE


MAX_NAME_LENGTH = 180


class InputFocus:
    def __init__(self, direct, log, path=STATE / "input-focus.json"):
        self.direct = direct
        self.log = log
        self.path = Path(path)
        self.names_path = self.path.with_name(f"{self.path.stem}-names.json")
        self.names = self._load_names()
        self.target = self._load()
        self.available = self.target is not None and self.target.get("kind") != "closed"

    def _load_names(self):
        try:
            names = json.loads(self.names_path.read_text(encoding="utf-8"))
            if not isinstance(names, dict):
                raise ValueError("expected a list of terminal names")
            return {ident: name for ident, name in names.items() if isinstance(name, str) and name}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.log.warning("Terminal names: %s", exc)
            return {}

    def _load(self):
        try:
            target = json.loads(self.path.read_text(encoding="utf-8"))
            if target is None or (isinstance(target, dict) and target.get("id") and target.get("kind")):
                return target
        except FileNotFoundError:
            return self._load_leader()
        except (OSError, ValueError) as exc:
            self.log.warning("Input lock: %s", exc)
        return {"id": "unavailable", "kind": "closed", "label": "Saved terminal"}

    def _load_leader(self):
        try:
            old = json.loads(self.path.with_name("leader.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(old, dict) or not old.get("enabled"):
            return None
        tty, pid, birth = old.get("tty"), old.get("pid"), old.get("birth")
        target = None
        if tty and type(pid) is int and birth and orchestration._process_start(pid) == birth:
            target = next((row for row in self.candidates("applescript")
                           if row.get("tty") == tty and row.get("bundle") == "com.apple.Terminal"), None)
        if target is None:
            target = {"id": "previous-terminal", "kind": "closed", "label": "Previously selected terminal"}
        try:
            self._save(target)
        except OSError as exc:
            self.log.warning("Input lock: could not save the previous terminal: %s", exc)
        return target

    def _save(self, data, path=None):
        path = self.path if path is None else path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _orca_id(pane):
        identity = [pane.get(key) for key in ("executionHostId", "worktreeId", "paneKey", "ptyId")]
        identity.append(pane.get("incarnationId") or pane["handle"])
        return "orca:" + json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    def _native_candidates(self):
        return [{**row, "kind": "native"} for row in native_input_focus.list_input_terminals(self.direct)]

    def candidates(self, route):
        candidates = self._native_candidates() if route != "orca" else []
        if route in ("orca", "smart") and self.direct.is_running(orca_cli.ORCA_BUNDLE):
            try:
                panes = orca_cli.list_input_terminals()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.log.debug("Input lock: Orca inventory: %s", exc)
                panes = []
            for pane in panes:
                label = " · ".join(filter(None, ("Orca", pane.get("worktree"), pane.get("title") or pane.get("agent"))))
                candidates.append({**pane, "kind": "orca", "id": self._orca_id(pane), "label": label})
        return candidates

    def _label(self, target):
        name = self.names.get(target["id"])
        if not name:
            return target.get("label")
        if target["kind"] == "orca":
            return " · ".join(filter(None, ("Orca", target.get("worktree"), name)))
        return " · ".join(filter(None, (target.get("label"), name)))

    def name_for(self, term, tty=""):
        names = self.names
        if not names:
            return ""
        if term.get("id"):
            return names.get(term["id"], "")
        if not term.get("via"):
            return names.get(self._orca_id(term), "") if term.get("handle") else ""
        bundle, pid = term.get("handle"), term.get("pid")
        if tty and any(ident.startswith("native:") for ident in names):
            matches = [row["id"] for row in self._native_candidates()
                       if (row["bundle"], row["pid"], row["tty"]) == (bundle, pid, tty)]
            if len(matches) == 1:
                return names.get(matches[0], "")
        return ""

    def summary(self):
        target = self.target
        return {"selected": target["id"] if target else None,
                "selected_label": self._label(target) if target else None,
                "available": bool(target and self.available)}

    def state(self, route):
        candidates = self.candidates(route)
        if self.target:
            self.available = any(row["id"] == self.target["id"] for row in candidates) or self.resolve() is not None
        return {**self.summary(), "candidates": [
            {"id": row["id"], "label": self._label(row), "custom_name": self.names.get(row["id"], "")}
            for row in candidates
        ]}

    def _candidate(self, route, ident):
        if not isinstance(ident, str) or not ident:
            raise ValueError("Choose a terminal from the list")
        target = next((row for row in self.candidates(route) if row["id"] == ident), None)
        if not target:
            raise ValueError("The terminal is unavailable. Refresh the list and choose it again")
        return target

    def set_name(self, route, ident, name):
        if not isinstance(name, str):
            raise ValueError("Enter a terminal name")
        name = name.strip()
        if len(name) > MAX_NAME_LENGTH or any(ord(char) < 32 for char in name):
            raise ValueError(f"The terminal name must be one line and at most {MAX_NAME_LENGTH} characters")
        self._candidate(route, ident)
        if self.names.get(ident, "") == name:
            return ""
        names = self.names.copy()
        if name:
            names[ident] = name
        else:
            names.pop(ident, None)
        self._save(names, self.names_path)
        self.names = names
        return name

    def select(self, route, ident):
        target = None if ident is None else self._candidate(route, ident)
        if (self.target or {}).get("id") == ident:
            return False
        self._save(target)
        self.target = target
        self.available = target is not None
        return True

    def resolve(self):
        target = self.target
        if not target:
            return None
        term = None
        if target["kind"] == "native":
            term = native_input_focus.resolve_input_terminal(self.direct, target)
        elif target["kind"] == "orca" and self.direct.is_running(orca_cli.ORCA_BUNDLE):
            term = orca_cli.resolve_input_terminal(target)
        self.available = term is not None
        if term and target["kind"] == "orca":
            target["handle"] = term["handle"]
        return term
