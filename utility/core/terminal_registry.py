"""Durable ownership captured at the TUI transport boundary, never from text/focus."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import time

DEFAULT = Path(__file__).resolve().parent.parent / 'data/state/terminal-owners.sqlite'


class TerminalRegistry:
    def __init__(self, path=DEFAULT):
        self.path = Path(path)

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        os.chmod(self.path, 0o600)
        db.execute('PRAGMA busy_timeout=2000')
        db.execute('CREATE TABLE IF NOT EXISTS owners (session TEXT, turn TEXT, tty TEXT, pid INTEGER, birth REAL, connection TEXT, at REAL, PRIMARY KEY(session,turn))')
        return db

    def bind(self, session, turn, owner):
        if not isinstance(session, str) or not session or len(session) > 200:
            return
        if not owner.get('tty') or not owner.get('pid') or not owner.get('birth'):
            return
        with closing(self._db()) as db, db:
            db.execute('INSERT OR REPLACE INTO owners VALUES (?,?,?,?,?,?,?)',
                       (session, turn or '', owner['tty'], owner['pid'], owner['birth'], owner['connection'], time.time()))
            # Keep bounded metadata; no prompts/replies or credentials are stored.
            db.execute('DELETE FROM owners WHERE at < ?', (time.time() - 30 * 86400,))

    def resolve(self, session, turn, sessions, processes):
        if not self.path.exists():
            return None
        try:
            with closing(self._db()) as db:
                row = db.execute('SELECT tty,pid,birth FROM owners WHERE session=? AND turn=?',
                                 (session, turn or '')).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if not row:
            return None
        tty, pid, birth = row
        if pid not in processes or processes[pid][1] != birth:
            return None
        matches = [s for s in sessions if s.get('agent') == 'codex'
                   and s.get('tty') == tty and s.get('pid') == pid]
        return dict(matches[0]) if len(matches) == 1 else None

    def sessions_for_terminal(self, tty, pid, processes):
        """Exact live session IDs for status observation; no prompt/content lookup."""
        if not self.path.exists() or pid not in processes:
            return []
        try:
            with closing(self._db()) as db:
                rows = db.execute('SELECT session,birth FROM owners WHERE tty=? AND pid=? AND turn=? ORDER BY at DESC',
                                  (tty, pid, '')).fetchall()
            return [sid for sid, birth in rows if birth == processes[pid][1]]
        except (OSError, sqlite3.Error):
            return []
