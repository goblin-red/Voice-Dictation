"""Voice assignments belong to a live terminal/agent process, never an agent type."""
import copy
import json
import os
import threading
from pathlib import Path

ENGINES = {'', 'system', 'silero', 'grok', 'openai', 'gemini'}

class TerminalVoiceProfiles:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            value = json.loads(self.path.read_text())
            self.profiles = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.profiles = {}

    @staticmethod
    def key(session):
        return session["tty"].removeprefix("/dev/")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.profiles, ensure_ascii=False))
        tmp.replace(self.path)

    @staticmethod
    def _owner_alive(profile):
        try:
            pid = int(profile.get("owner_pid", 0))
            if pid <= 1:
                return False
            os.kill(pid, 0)
            return True
        except (ValueError, TypeError, OSError):
            return False

    def sync(self, sessions):
        live = {self.key(s): s for s in sessions}
        with self.lock:
            kept = {k:v for k,v in self.profiles.items()
                    if (k in live and v.get("owner_pid") == live[k]["pid"])
                    or (k not in live and self._owner_alive(v))}
            if kept != self.profiles:
                old = self.profiles
                self.profiles = kept
                try: self._save()
                except OSError:
                    self.profiles = old
                    raise
            return copy.deepcopy(kept)

    def assign(self, session_id, profile, sessions):
        if not any((s.get('session_id') == session_id or self.key(s) == session_id) for s in sessions):
            raise ValueError('This terminal closed. Refresh the list')
        if not isinstance(profile, dict) or profile.get('engine', '') not in ENGINES:
            raise ValueError('Invalid speech engine')
        voices = profile.get('voices', {})
        if not isinstance(voices, dict): raise ValueError('Invalid voice')
        for engine, value in voices.items():
            if engine not in {'system','silero','grok','openai'} or not isinstance(value,str) or len(value)>100:
                raise ValueError('Invalid voice')
            if value and engine=='silero' and value not in {'xenia','kseniya','baya','aidar','eugene'}:
                raise ValueError('Unknown Silero voice')
        current = next(s for s in sessions if (s.get('session_id') == session_id or self.key(s) == session_id))
        key = self.key(current)
        with self.lock:
            old = copy.deepcopy(self.profiles)
            if profile.get('engine'):
                self.profiles[key] = {'engine':profile['engine'], 'voices':dict(voices), 'owner_pid':current['pid']}
            else:
                self.profiles.pop(key, None)
            try: self._save()
            except OSError:
                self.profiles = old
                raise
            return copy.deepcopy(self.profiles.get(key, {}))

    def for_event(self, event, sessions):
        from terminal_voice import event_tty
        profiles = self.sync(sessions)
        tty = event_tty(event, sessions)
        if not tty: return {}
        matching = [s for s in sessions if s.get('tty') == tty and s.get('agent') == event.get('agent')]
        return profiles.get(self.key(matching[0]), {}) if len(matching)==1 else {}
