"""Attach terminal identity from process metadata, never from answer contents."""
import datetime
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from orca_answer_source import resolve_orca_answer
from terminal_voice import SESSION_ID, event_tty


def processes():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart='],
                            env={**os.environ, 'LC_ALL': 'C'}, capture_output=True,
                            text=True, timeout=3, check=True)
    found = {}
    for row in result.stdout.splitlines():
        parts = row.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            started = datetime.datetime.strptime(parts[2].strip(), '%a %b %d %H:%M:%S %Y').timestamp()
            found[int(parts[0])] = (int(parts[1]), started)
        except ValueError:
            continue
    return found


class TerminalSources:
    def __init__(self, database=None, snapshot=processes, registry=None):
        self.database = Path(database or Path.home()/'.codex/logs_2.sqlite')
        self.snapshot = snapshot
        self.registry = registry

    def enrich(self, event, sessions):
        if event.get('paneKey'):
            return dict(event)
        agent = event.get('agent')
        path = event.get('path') or ''
        match = SESSION_ID.search(Path(path).name)
        # The transport registry is authoritative for shared-server Codex sessions.
        if self.registry is not None and agent == 'codex' and match:
            owner = self.registry.resolve(match.group(1).lower(), event.get('turn_id'),
                                          sessions, self.snapshot())
            if owner:
                return {**event, 'tty': owner['tty'], 'pid': owner['pid'],
                        'terminal_source': 'registered-transport'}
        tty = event_tty(event, sessions)
        candidates = [s for s in sessions if s.get('agent') == agent and s.get('tty') == tty] if tty else []
        if not candidates and agent == 'codex':
            candidates = self._codex_sources(event, sessions)
        if len(candidates) == 1:
            source = candidates[0]
            return {**event, 'tty': source['tty'], 'pid': source['pid'],
                    'terminal_source': 'process-metadata'}
        if (not candidates and agent in ('claude', 'codex') and match
                and event.get('source') != 'hook' and (event.get('interim') or event.get('state') == 'done')):
            pane = resolve_orca_answer(agent, match.group(1), path)
            if pane:
                # Orca status and Stop hooks still drive completion for this pane.
                return {**event, **pane, 'source': 'log', 'terminal_source': 'orca-transcript',
                        'work_complete': False}
        return dict(event)

    def _codex_sources(self, event, sessions):
        match = SESSION_ID.search(Path(event.get('path') or '').name)
        if not match or not self.database.is_file():
            return []
        try:
            procs = self.snapshot()
            with closing(sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
                rows = db.execute('SELECT process_uuid, MAX(ts) FROM logs WHERE thread_id = ? GROUP BY process_uuid',
                                  (match.group(1).lower(),)).fetchall()
        except (OSError, subprocess.SubprocessError, sqlite3.Error):
            return []
        owners = set()
        for identity, timestamp in rows:
            pid_match = re.fullmatch(r'pid:(\d+):[^:]+', identity or '')
            if not pid_match:
                continue
            pid = int(pid_match.group(1))
            if pid not in procs or timestamp < procs[pid][1]:
                continue  # stale record from a previous process with the same PID
            ancestry = set()
            while pid > 1 and pid in procs and pid not in ancestry:
                ancestry.add(pid)
                pid = procs[pid][0]
            for session in sessions:
                if session.get('agent') == 'codex' and session['pid'] in ancestry:
                    owners.add((session['tty'], session['pid']))
        return [s for s in sessions if (s['tty'], s['pid']) in owners]
