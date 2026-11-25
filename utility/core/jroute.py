#!/usr/bin/env python3
"""jroute list | details [SESSION_ID or TTY_ID] | ready CODE | bind SESSION_ID | send --context CONTEXT_ID SESSION_ID 'text' | send --context CONTEXT_ID SESSION_ID - < file

One-way literal delivery for the selected Voice leader. No gbus, no reply channels.
"""
import json
import os
import sys
import urllib.request
import urllib.error
import uuid


def main(args):
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    cmd = args[0]
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = ''
    data = {'pid': os.getpid(), 'tty': tty, 'rid': str(uuid.uuid4()),
            'session': os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('OPENCODE_SESSION_ID') or '',
            'term_session': os.environ.get('TERM_SESSION_ID', '')}
    base = 'http://127.0.0.1:8792/api/leader/'
    if (cmd == 'list' and len(args) == 1) or (cmd == 'details' and len(args) in (1, 2)):
        request = urllib.request.Request(base + 'state')
    elif cmd in ('ready', 'bind') and len(args) == 2:
        data['nonce' if cmd == 'ready' else 'to'] = args[1]
        request = urllib.request.Request(base + cmd, json.dumps(data).encode(), {'Content-Type': 'application/json'})
    elif cmd == 'send' and len(args) == 5 and args[1] == '--context':
        data.update(context_id=args[2], to=args[3], text=sys.stdin.read() if args[4] == '-' else args[4])
        request = urllib.request.Request(base + 'send', json.dumps(data, ensure_ascii=False).encode(), {'Content-Type': 'application/json'})
    else:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read())
        except ValueError:
            result = {'ok': False, 'error': str(exc)}
    except (OSError, ValueError) as exc:
        result = {'ok': False, 'error': str(exc) + '. The message was not retried'}
    if cmd in ('list', 'details') and result.get('ok') is not False and not result.get('error'):
        if cmd == 'list':
            leader = result.get('leader') or {}
            compact = {k: leader.get(k) for k in ('session_id', 'context_id', 'ready')}
            if leader.get('error'):
                compact['error'] = leader['error']
            result = {'leader': compact, 'sessions': [
                {k: row.get(k) for k in ('session_id', 'nick', 'description')}
                for row in result.get('sessions', [])]}
        elif len(args) == 2:
            rows = [row for row in result.get('sessions', [])
                    if args[1] in (row.get('session_id'), row.get('id'), row.get('tty'))]
            if len(rows) != 1:
                result = {'ok': False, 'error': 'Could not identify a single terminal. Run jroute details'}
            else:
                result = {'leader': result.get('leader'), 'sessions': rows}
    print(json.dumps(result, ensure_ascii=False, **({'separators': (',', ':')} if cmd == 'list' else {'indent': 2})))
    return 0 if result.get('ok', cmd in ('list', 'details')) else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
