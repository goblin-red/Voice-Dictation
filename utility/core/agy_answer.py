"""Send completed Antigravity CLI replies to Voice from its passive Stop hook."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import uuid

from answer_hook import MAX_BYTES, SOCKET, process_table


def _private_voice_session(data):
    paths = data.get('workspacePaths') or data.get('workspace_paths')
    if not isinstance(paths, list):
        paths = []
    temporary = Path(tempfile.gettempdir()).resolve()
    for value in [data.get('cwd'), *paths]:
        if isinstance(value, str) and value:
            path = Path(value).resolve()
            if path.parent == temporary and path.name.startswith('voice-agy-session-'):
                return True
    return False


def _last_reply(path, session):
    if not isinstance(path, str) or not path:
        return None
    # Current agy transcripts are inside the named conversation's brain directory.
    directory = (Path.home() / '.gemini/antigravity-cli/brain' / session
                 / '.system_generated/logs').resolve()
    transcript = Path(path).resolve()
    if (transcript.parent != directory
            or transcript.name not in ('transcript.jsonl', 'transcript_full.jsonl')
            or not transcript.is_file()):
        return None
    try:
        with transcript.open('rb') as stream:
            start = max(0, stream.seek(0, os.SEEK_END) - 4 * MAX_BYTES)
            stream.seek(start)
            lines = stream.read(4 * MAX_BYTES).splitlines()
    except OSError:
        return None
    if start:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        kind, source = record.get('type'), record.get('source')
        if kind in ('USER_INPUT', 'REQUEST') and source in ('USER', 'USER_EXPLICIT'):
            return None
        if kind == 'PLANNER_RESPONSE' and source == 'MODEL':
            text = record.get('content')
            if (record.get('status') not in (None, 'DONE') or record.get('tool_calls')
                    or not isinstance(text, str) or not text.strip()):
                return None
            step = record.get('step_index')
            return text, ('step:' + str(step) if type(step) is int else None)
    return None


def main():
    data = json.loads(sys.stdin.buffer.read(MAX_BYTES))
    event = data.get('hookEventName') or data.get('hook_event_name')
    reason = data.get('terminationReason') or data.get('termination_reason')
    if (event != 'Stop' or data.get('fullyIdle') is False or data.get('fully_idle') is False
            or reason in ('ERROR', 'USER_CANCELED') or not SOCKET.exists()
            or os.environ.get('GOBLIN_VOICE_INTERNAL_AGY') == '1' or _private_voice_session(data)):
        return
    session = data.get('conversationId') or data.get('conversation_id')
    if not isinstance(session, str):
        return
    session = str(uuid.UUID(session))
    path = data.get('transcriptPath') or data.get('transcript_path') or ''
    reply = _last_reply(path, session)
    text = data.get('last_assistant_message') or data.get('lastAssistantMessage')
    if not isinstance(text, str) or not text.strip():
        if not reply:
            return
        text = reply[0]
    turn = data.get('turnId') or data.get('turn_id')
    if not turn and reply and text.strip() == reply[0].strip():
        # The execution number belongs to a continuation, and repeats across user turns.
        turn = reply[1]
    procs, ancestors, pid = process_table(), [], os.getppid()
    while pid > 1 and pid in procs and len(ancestors) < 32:
        parent, birth = procs[pid]
        ancestors.append([pid, birth])
        pid = parent
    payload = {'agent': 'agy', 'session': session, 'message': text,
               'path': path, 'cwd': data.get('cwd') or '', 'ancestors': ancestors,
               'at': time.time(), 'turn': turn, 'delivery': uuid.uuid4().hex}
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
        pass  # This observer must never prevent agy from finishing the turn.
    print('{}')
