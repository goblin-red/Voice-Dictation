"""Speech from remote Codex TUI processes, without guessing thread ownership.

The shared server owns rollout files, but each TUI logs its own rendered agent
stream under a process UUID. This adapter reads only that stream, never matches
answer text against a different source, and never registers gateway identities.
"""
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from contextlib import closing


class CodexTerminalStream:
    def __init__(self, log, database=None):
        self.log = log
        self.database = Path(database or Path.home() / '.codex/logs_2.sqlite')
        self.cursor = None
        self.buffers = {}
        self.remote = {}
        self.scanned = 0
        self.warned = False

    def _terminals(self, sessions):
        live = {s['pid']: s for s in sessions if s.get('agent') == 'codex'}
        if time.monotonic() - self.scanned >= 2:
            from orchestration import _process_start
            remote = {}
            for pid, side in live.items():
                try:
                    out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                         capture_output=True, text=True, timeout=2)
                    # Only remote TUI streams replace shared-server rollouts.
                    if out.returncode == 0 and re.search(r'(?:^|\s)--remote(?:=|\s)', out.stdout):
                        born = _process_start(pid)
                        if born:
                            stamp = time.mktime(time.strptime(born, '%a %b %d %H:%M:%S %Y'))
                            remote[pid] = (side, stamp)
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
            self.remote = remote
            self.scanned = time.monotonic()
        self.remote = {pid: (live[pid], born) for pid, (side, born) in self.remote.items()
                       if pid in live and live[pid]['tty'] == side['tty']}
        return self.remote

    def filter_logs(self, events, include_interim=False):
        """Only the fast reader consumes TUI deltas; slow polling cannot replay them."""
        remote = self.remote
        if not remote:
            return events
        return [ev for ev in events if ev.get('paneKey') or (ev.get('pid') and (
            ev['pid'] not in remote or (include_interim and ev.get('interim') and ev.get('tty'))))]

    def route(self, events, sessions):
        poll_at = time.time()
        previous_poll = getattr(self, "_last_poll_at", poll_at)
        self._last_poll_at = poll_at
        remote = self._terminals(sessions)
        if not remote:
            self.buffers.clear()
            self.cursor = None
            return events
        # A remote rollout has no trustworthy terminal owner. Its replacement
        # is the TUI stream below; retaining it would speak the same reply twice.
        kept = [ev for ev in events if ev.get('pid') and ev['pid'] not in remote]
        try:
            with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
                latest = db.execute('SELECT MAX(id) FROM logs').fetchone()[0] or 0
                if self.cursor is None or latest < self.cursor:
                    self.cursor = latest  # Never replay old answers when Voice starts.
                    self.buffers.clear()
                    return kept
                rows = db.execute("SELECT id,ts,ts_nanos,process_uuid,target,feedback_log_body FROM logs "
                                  "WHERE id>? AND id<=? AND target IN ('codex_tui::markdown_stream',"
                                  "'codex_tui::app::agent_message_consolidation') ORDER BY id",
                                  (self.cursor, latest)).fetchall()
                self.cursor = latest
        except (OSError, sqlite3.Error):
            if not self.warned:
                self.log.warning('Speech: the remote Codex stream is unavailable')
                self.warned = True
            return kept
        self.warned = False
        for ident, stamp, nanos, process, target, body in rows:
            match = re.fullmatch(r'pid:(\d+):[^:]+', process or '')
            pid = int(match.group(1)) if match else None
            if pid not in remote or stamp < remote[pid][1]:
                continue
            side = remote[pid][0]
            key = (process, remote[pid][1])
            buf = self.buffers.setdefault(key, {'text': '', 'length': None, 'invalid': False})
            body = body or ''
            if target == 'codex_tui::markdown_stream' and body.startswith('push_delta: '):
                try:
                    delta = json.loads(body[len('push_delta: '):])
                    if not isinstance(delta, str):
                        raise ValueError('non-text delta')
                    buf['text'] += delta
                    if len(buf['text']) > 200000:
                        buf['invalid'] = True
                        buf['text'] = ''
                except ValueError:
                    buf['invalid'] = True
            elif target == 'codex_tui::app::agent_message_consolidation':
                length = re.search(r'\bsource_len=(\d+)', body)
                if length:
                    buf['length'] = int(length.group(1))
                if 'replacing cells ' in body and ' with AgentMarkdownCell' in body:
                    text = buf['text']
                    # Partial streams after startup/rotation are discarded.
                    if text.strip() and not buf['invalid'] and buf['length'] == len(text.encode('utf-8')):
                        self.log.debug("Speech stream: tty=%s entry_age=%.3f s poll_interval=%.3f s read=%.3f s",
                                       side['tty'], time.time() - stamp - nanos / 1e9,
                                       poll_at - previous_poll, time.time() - poll_at)
                        # Rendered message completion is NOT turn completion (commentary uses it too).
                        kept.append({'agent': 'codex', 'state': 'done', 'message': text,
                                     'tty': side['tty'], 'pid': pid, 'cwd': side.get('cwd'),
                                     'path': side.get('log'), 'terminal_source': 'codex-tui-stream',
                                     'source_event_id': ident, 'work_complete': False})
                    self.buffers.pop(key, None)
        alive = {pid for pid in remote}
        self.buffers = {key: value for key, value in self.buffers.items()
                        if int(key[0].split(':')[1]) in alive}
        return kept
