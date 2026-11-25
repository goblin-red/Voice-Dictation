"""Passive Stop hook: send final text to Voice, never instruct or block the agent."""
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

SOCKET = Path(__file__).resolve().parent.parent / 'data/state/answer-hooks.sock'
MAX_BYTES = 1024 * 1024


def process_table():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart='],
                            capture_output=True, text=True, timeout=1,
                            env={**os.environ, 'LC_ALL': 'C'}, check=True)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                rows[int(parts[0])] = (int(parts[1]), datetime.datetime.strptime(
                    parts[2].strip(), '%a %b %d %H:%M:%S %Y').timestamp())
            except ValueError:
                pass
    return rows


def main():
    data = json.loads(sys.stdin.buffer.read(MAX_BYTES))
    agent = sys.argv[1]
    # Grok also loads Claude's settings: only its native registration sends.
    if agent == 'claude' and 'hookEventName' in data:
        return
    event = data.get('hook_event_name') or data.get('hookEventName')
    if event not in ('Stop', 'stop') or data.get('subagentType'):
        return
    if agent == 'grok' and data.get('reason') not in (None, 'end_turn'):
        return
    text = data.get('last_assistant_message') or data.get('lastAssistantMessage')
    session = data.get('session_id') or data.get('sessionId')
    if not isinstance(text, str) or not text.strip() or not session:
        return
    if not SOCKET.exists():
        return
    procs, ancestors, pid = process_table(), [], os.getppid()
    while pid > 1 and pid in procs and len(ancestors) < 32:
        parent, birth = procs[pid]
        ancestors.append([pid, birth])
        pid = parent
    payload = {'agent': agent, 'session': session, 'message': text,
               'path': data.get('transcript_path') or data.get('transcriptPath') or '',
               'cwd': data.get('cwd') or '', 'ancestors': ancestors, 'at': time.time(),
               'turn': data.get('turn_id') or data.get('prompt_id') or data.get('promptId'),
               'delivery': uuid.uuid4().hex}
    raw = json.dumps(payload, ensure_ascii=False).encode()
    if len(raw) > MAX_BYTES:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(.25)
        conn.connect(str(SOCKET))
        conn.sendall(raw)
        conn.shutdown(socket.SHUT_WR)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass  # Voice being closed must never prevent a CLI turn from completing.
    print('{}')  # Valid passive Stop output in all three clients.
