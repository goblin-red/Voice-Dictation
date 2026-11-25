"""Per-TUI WebSocket relay: captures RPC ownership, forwards original messages.

Run only through codex-voice-remote. The upstream daemon is not changed. Each
TUI has a private Unix socket and an independently verified process lifetime.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import tempfile
import uuid

from websockets.asyncio.client import connect, unix_connect
from websockets.asyncio.server import unix_serve
from answer_hook import process_table
from terminal_registry import TerminalRegistry


class Ownership:
    def __init__(self, registry, owner):
        self.registry, self.owner = registry, owner
        self.pending = {}

    def client(self, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict) or 'id' not in msg:
            return
        method, params = msg.get('method'), msg.get('params') or {}
        if method not in ('thread/start', 'thread/resume', 'thread/fork', 'turn/start'):
            return
        if len(self.pending) >= 1024:
            raise RuntimeError('too many pending ownership requests')
        self.pending[msg['id']] = (method, params.get('threadId'))
        # Don't claim a turn until the server accepts it.

    def server(self, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if 'method' in msg:
            return  # Server requests have an independent ID namespace.
        request = self.pending.pop(msg.get('id'), None) if 'id' in msg else None
        if not request or not isinstance(msg.get('result'), dict):
            return
        method, session = request
        result = msg['result']
        if method == 'turn/start':
            turn = (result.get('turn') or {}).get('id')
            if session and turn:
                self.registry.bind(session, turn, self.owner)
                self.registry.bind(session, '', self.owner)
        else:
            # Session created/resumed through this exact client connection.
            # A resume alone must not steal an existing in-flight turn.
            thread = result.get('thread') or {}
            session = thread.get('id')
            if session and method in ('thread/start', 'thread/fork'):
                self.registry.bind(session, '', self.owner)


def remote_args(args, address):
    out, found, i = [], False, 0
    while i < len(args):
        arg = args[i]
        if arg == '--remote':
            if i + 1 == len(args):
                raise ValueError('--remote needs an address')
            found = True
            out.extend(['--remote', address]); i += 2
        elif arg.startswith('--remote='):
            found = True
            out.extend(['--remote', address]); i += 1
        else:
            out.append(arg); i += 1
    return out if found else ['--remote', address, *out]


def upstream_address(args):
    for i, arg in enumerate(args):
        if arg == '--remote' and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith('--remote='):
            return arg.split('=', 1)[1]
    return 'unix://'


async def run(executable, args, registry=None):
    upstream = upstream_address(args)
    tty = os.ttyname(sys.stdin.fileno())
    ready = asyncio.Event()
    child = None
    owner = {}
    registry = registry or TerminalRegistry()
    active = False

    async def relay(ws):
        nonlocal active
        await ready.wait()
        sock = ws.transport.get_extra_info('socket')
        # macOS LOCAL_PEERPID: only our child can use this endpoint.
        peer = struct.unpack('i', sock.getsockopt(0, 2, 4))[0]
        if active or peer != child.pid:
            await ws.close(1008, 'wrong terminal process')
            return
        active = True
        ownership = Ownership(registry, owner)
        headers = {}
        for key in ('Authorization',):
            value = ws.request.headers.get(key)
            if value:
                headers[key] = value
        if upstream.startswith('unix://'):
            path = upstream[7:] or str(Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')) / 'app-server-control/app-server-control.sock')
            connection = unix_connect(path, uri='ws://localhost', additional_headers=headers,
                                      max_size=None, proxy=None)
        elif upstream.startswith(('ws://', 'wss://')):
            connection = connect(upstream, additional_headers=headers, max_size=None)
        else:
            raise ValueError('unsupported upstream address')
        try:
            async with connection as server:
                async def copy(source, target, inspect):
                    async for raw in source:
                        inspect(raw)
                        await target.send(raw)
                tasks = [asyncio.create_task(copy(ws, server, ownership.client)),
                         asyncio.create_task(copy(server, ws, ownership.server))]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
        finally:
            active = False

    with tempfile.TemporaryDirectory(prefix='gvoice-') as tmp:
        path = str(Path(tmp)/'rpc.sock')
        async with unix_serve(relay, path, max_size=None):
            os.chmod(path, 0o600)
            child = await asyncio.create_subprocess_exec(executable, *remote_args(args, 'unix://'+path))
            table = process_table()
            if child.pid not in table:
                raise RuntimeError('terminal process exited before registration')
            owner.update(tty=tty, pid=child.pid, birth=table[child.pid][1], connection=uuid.uuid4().hex)
            ready.set()
            # Terminal sends SIGINT to the foreground process group; the TUI handles it.
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, lambda: None)
            loop.add_signal_handler(signal.SIGTERM, child.terminate)
            return await child.wait()


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(run(sys.argv[1], sys.argv[2:])))
    except Exception as exc:
        print('Voice terminal transport: '+str(exc), file=sys.stderr)
        sys.exit(1)
