"""Independent work clocks, identified by terminal process (or Orca pane).

No audio/OS side effects. The application supplies observations and emits one
pending ping at a time, so simultaneous deadlines don't collapse into one sound.
"""
import threading
import time


class WorkTimers:
    BUSY = {'busy', 'working', 'running', 'thinking'}
    IDLE = {'idle', 'done', 'stopped', 'waiting', 'blocked', 'ready'}

    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.lock = threading.RLock()
        self.records = {}
        self.observed = {}
        self.jobs = {}
        self.scope = None
        self.period = None
        self.last_ping = None

    @staticmethod
    def key(event):
        if event.get('paneKey'):
            return ('pane', event['paneKey'])
        if event.get('tty') and event.get('pid'):
            return ('tty', event['tty'], event['pid'])
        return None

    def configure(self, scope, period):
        with self.lock:
            if (scope, period) != (self.scope, self.period):
                self.scope, self.period = scope, period
                # A changed mode/interval starts a fresh interval, not a burst.
                now = self.clock()
                for record in self.records.values():
                    record['next'] = now + period

    def start(self, event, observed=False):
        key = self.key(event)
        if key is None:
            return False
        with self.lock:
            if key in self.records:
                self.records[key]['observed_busy'] |= observed
                if event.get('work_id'):
                    self.records[key]['work_id'] = event['work_id']
                if event.get('work_turn_ids'):
                    self.records[key]['work_turn_ids'] = list(event['work_turn_ids'])
                return False  # Voice Enter and a status update describe one job.
            if not observed:
                self.jobs.pop(key, None)
            now = self.clock()
            self.records[key] = dict(event, at=now, wall=self.wall(),
                                     next=now + (self.period or 15), observed_busy=observed)
            return True

    def finish(self, event):
        key = self.key(event)
        with self.lock:
            record = self.records.get(key)
            if (record is None or event.get('interim') or not event.get('work_complete', True)
                    or event.get('terminal_source') == 'codex-tui-stream'):
                return False
            if event.get('completed_at') and event['completed_at'] < record['wall']:
                return False
            turns = record.get('work_turn_ids') or []
            if event.get('turn_id') and turns:
                if event['turn_id'] not in turns:
                    return False
                remaining = [turn for turn in turns if turn != event['turn_id']]
                if remaining:
                    record['work_turn_ids'] = remaining
                    return False  # Another task in this terminal is still running.
            self.records.pop(key)
            # Ignore the scanner's stale busy snapshot until it reports idle.
            self.observed[key] = 'busy'
            self.jobs.setdefault(key, None)
            return True

    def clear(self, reset=False):
        with self.lock:
            for key in self.records:
                self.observed[key] = 'busy'
                self.jobs.setdefault(key, None)
            self.records.clear()
            if reset:
                self.observed.clear()
                self.jobs.clear()

    def observe(self, sessions, max_age):
        with self.lock:
            live = {self.key(s) for s in sessions}
            now = self.clock()
            busy = {self.key(s) for s in sessions if str(s.get('state') or '').lower() in self.BUSY}
            for key, record in list(self.records.items()):
                if (key[0] == 'tty' and key not in live) or (key not in busy and now - record['at'] > max_age):
                    self.records.pop(key)
            self.observed = {k: v for k, v in self.observed.items() if k in live or k[0] == 'pane'}
            self.jobs = {k: v for k, v in self.jobs.items() if k in live or k[0] == 'pane'}
            for side in sessions:
                key = self.key(side)
                if key is None:
                    continue
                state = str(side.get('state') or '').lower()
                old = self.observed.get(key)
                if state in self.BUSY:
                    job = side.get('work_id')
                    blocked_first_snapshot = key in self.jobs and self.jobs[key] is None
                    if not blocked_first_snapshot and (old != 'busy' or (job and self.jobs.get(key) != job)):
                        self.start(side, observed=True)
                    elif key in self.records:
                        self.records[key]['observed_busy'] = True
                    self.observed[key] = 'busy'
                    if job:
                        self.jobs[key] = job
                elif state in self.IDLE:
                    self.observed[key] = 'idle'
                    if self.jobs.get(key) is None:
                        self.jobs.pop(key, None)
                    record = self.records.get(key)
                    # A just-submitted request can precede the scanner's busy state.
                    if record and record['observed_busy']:
                        self.records.pop(key)

    def next_due(self, focus=None):
        with self.lock:
            now = self.clock()
            if self.scope == 'all' and self.last_ping is not None and now < self.last_ping + (self.period or 15):
                return None
            records = [(k, r) for k, r in self.records.items() if r['next'] <= now]
            if self.scope != 'all':
                focus = focus or {}
                pane = (focus.get('orca') or {}).get('paneKey')
                records = [(k, r) for k, r in records
                           if (focus.get('tty') and r.get('tty') == focus['tty'])
                           or (pane and r.get('paneKey') == pane)]
            return min(records, key=lambda pair: pair[1]['next'])[0] if records else None

    def claim(self, key):
        with self.lock:
            record = self.records.get(key)
            if record is None:
                return None
            period = self.period or 15
            now = self.clock()
            if self.scope == 'all':
                if self.last_ping is not None and now < self.last_ping + period:
                    return None
                # One audible heartbeat represents all currently working terminals.
                self.last_ping = now
                for active in self.records.values():
                    active['next'] = now + period
                return dict(record)
            self.last_ping = now
            # Keep the original cadence; delayed playback must not accumulate drift.
            missed = max(1, int((now - record['next']) // period) + 1)
            record['next'] += missed * period
            return dict(record)
