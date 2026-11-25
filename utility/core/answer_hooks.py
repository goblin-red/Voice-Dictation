"""Local, user-private push channel for completed CLI replies (no transcript polling)."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import threading
import time

from answer_hook import SOCKET, MAX_BYTES, process_table
from orca_answer_source import resolve_orca_answer
from terminal_registry import TerminalRegistry


HOOK_AGENTS = frozenset(('codex', 'claude', 'grok', 'agy'))


class AnswerHooks:
    def __init__(self, watcher):
        self.watcher = watcher
        self.queue = queue.Queue(maxsize=128)
        self.seen = deque(maxlen=1024)
        self.sessions = lambda: []
        self.registry = TerminalRegistry()

    def run(self):
        SOCKET.parent.mkdir(parents=True, exist_ok=True)
        # Voice owns one instance (the app's normal single-instance guard).
        SOCKET.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(SOCKET))
            os.chmod(SOCKET, 0o600)
            server.listen(16)
            server.settimeout(.5)
            threading.Thread(target=self._consume, daemon=True, name='hook-speech').start()
            try:
                while not self.watcher._stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    try:
                        with conn:
                            conn.settimeout(.25)
                            parts, size = [], 0
                            while size <= MAX_BYTES:
                                part = conn.recv(min(65536, MAX_BYTES + 1 - size))
                                if not part:
                                    break
                                parts.append(part)
                                size += len(part)
                            if size > MAX_BYTES:
                                continue
                        self.queue.put_nowait(json.loads(b''.join(parts)))
                    except (OSError, ValueError, queue.Full):
                        self.watcher.log.warning('Hook: invalid event or queue full')
            finally:
                SOCKET.unlink(missing_ok=True)

    def _source(self, data):
        sessions = self.sessions()
        ev = {'agent': data['agent'], 'path': data.get('path') or '',
              'message': data['message'], 'cwd': data.get('cwd') or '',
              'state': 'done', 'source': 'hook', 'hook_session': data['session'],
              'completed_at': data.get('at'), 'turn_id': data.get('turn')}
        if data['agent'] == 'codex':
            owner = self.registry.resolve(data['session'], data.get('turn'), sessions, process_table())
            if owner:
                return {**ev, 'tty': owner['tty'], 'pid': owner['pid'],
                        'terminal_source': 'registered-transport'}
        if data['agent'] == 'grok' and not ev['path']:
            exact = [s for s in sessions if s.get('agent') == 'grok' and s.get('log')
                     and Path(s['log']).parent.name == data['session']]
            if len(exact) == 1:
                ev['path'] = exact[0]['log']
        # Resolve native transcript/session metadata first, including shared Codex daemons.
        if not ev['path'] and data['agent'] == 'codex':
            ev['path'] = data['session'] + '.jsonl'
        ev = self.watcher.resolve_source(ev)
        if ev.get('tty') and ev.get('pid'):
            self._save_source(data, ev)
            return ev
        procs = process_table()
        parents = {pid for pid, birth in data.get('ancestors', [])
                   if pid in procs and procs[pid][1] == birth}
        candidates = [s for s in sessions if s.get('agent') == data['agent'] and s.get('pid') in parents]
        if len(candidates) == 1:
            tab = candidates[0]
            ev = {**ev, 'tty': tab['tty'], 'pid': tab['pid'], 'terminal_source': 'hook-process'}
            self._save_source(data, ev)
            return ev
        # A native terminal's exact turn/process ownership takes precedence over
        # a provider session that may also have been opened in Orca.
        pane = resolve_orca_answer(data['agent'], data['session'], ev['path'])
        if pane:
            return {**ev, 'paneKey': pane['paneKey'], 'worktreeId': pane.get('worktreeId'),
                    'terminal_source': 'orca-hook'}
        return None  # Never guess by cwd, focus or text.

    def _save_source(self, data, ev):
        if data['agent'] != 'codex':
            return
        table = process_table()
        if ev['pid'] in table:
            owner = {'tty': ev['tty'], 'pid': ev['pid'], 'birth': table[ev['pid']][1],
                     'connection': 'native-process'}
            self.registry.bind(data['session'], data.get('turn'), owner)
            if data.get('turn'):
                self.registry.bind(data['session'], '', owner)

    def _consume(self):
        w = self.watcher
        while not w._stop.is_set():
            try:
                data = self.queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                if (not isinstance(data, dict) or data.get('agent') not in HOOK_AGENTS
                        or not isinstance(data.get('message'), str)
                        or not isinstance(data.get('session'), str) or not data['session']):
                    continue
                # Record availability even in log mode, without speaking the hook twice.
                w.state.setdefault('answer_hooks', {})[data['agent']] = time.time()
                key = (data['agent'], data['session'], data.get('turn') or data.get('delivery'),
                       hashlib.sha256(data['message'].encode()).hexdigest())
                if key in self.seen:
                    continue
                ev = self._source(data)
                # A newly opened terminal can be awaiting the inventory's async refresh.
                for delay in (.1, .3, .6):
                    if ev or w._stop.wait(delay):
                        break
                    ev = self._source(data)
                if not ev:
                    w.log.warning('Hook %s: unknown terminal for session %s; reply not read aloud',
                                  data['agent'], data['session'])
                    continue
                self.seen.append(key)
                w.log.debug('Hook: agent=%s tab=%s delivery=%.3f s',
                            ev['agent'], ev.get('paneKey') or ev.get('tty'), time.time() - data.get('at', time.time()))
                with w._dispatch_lock:
                    if w.cfg.get('answer_signal', 'log') == 'hooks':
                        w._remember(ev['agent'], ev)
                    sources = w.cfg.get('sources') or {}
                    if sources.get(ev['agent'], True) and (not ev.get('paneKey') or sources.get('orca', True)):
                        w._dispatch([ev])
            except Exception:
                w.log.exception('Hook: reply processing')
