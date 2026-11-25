"""Private persistent agy PTY, with no physical microphone or API credentials."""
import codecs, fcntl, json, os, pty, select, shlex, shutil, socket, struct
import subprocess, sys, tempfile, termios, threading, time
from pathlib import Path



class AgySession:
    def __init__(self, hooks=False, cancel=None):
        self.cancel=cancel
        import pyte
        self.tmp = tempfile.TemporaryDirectory(prefix='voice-agy-session-')
        self.root = Path(self.tmp.name)
        self.done_file = self.root / 'turn.done'
        self.stop_event = threading.Event()
        self.screen = pyte.Screen(240,160)
        self.stream = pyte.Stream(self.screen)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.server = socket.socket()
        self.server.bind(('127.0.0.1',0)); self.server.listen(8); self.server.settimeout(.2)
        self.p = None
        self.master = None
        try:
            if hooks:
                folder = self.root/'.agents'; folder.mkdir()
                hook = self.root/'hook.py'
                hook.write_text("import json,sys,pathlib\njson.load(sys.stdin)\n"
                                "if sys.argv[1]=='stop':pathlib.Path(__file__).with_name('turn.done').write_text('done')\n"
                                "print(json.dumps({'decision':'deny','reason':'Voice reader has no tools'} if sys.argv[1]=='deny' else {}))\n")
                cmd=shlex.quote(sys.executable)+' '+shlex.quote(str(hook))
                (folder/'hooks.json').write_text(json.dumps({'voice_reader':{
                    'Stop':[{'type':'command','command':cmd+' stop'}],
                    'PreToolUse':[{'matcher':'.*','hooks':[{'type':'command','command':cmd+' deny'}]}]}}))
            env=dict(os.environ)
            for key in ('GEMINI_API_KEY','GOOGLE_API_KEY','AGY_ADC_AUTH'):env.pop(key,None)
            settings=Path.home()/'.gemini/antigravity-cli/settings.json'
            if settings.exists() and json.loads(settings.read_text()).get('modelProvider') not in (None,'antigravity'):
                raise RuntimeError('Sign in to agy with your Google subscription')
            env.update(TERM='xterm-256color', ANTIGRAVITY_MIC='127.0.0.1:'+str(self.server.getsockname()[1]),
                       GOBLIN_VOICE_INTERNAL_AGY='1')
            self.master,slave=pty.openpty()
            try:
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',160,240,0,0))
                args=[shutil.which('agy') or str(Path.home()/'.local/bin/agy'),'--mode','plan']
                if hooks:args+=['--model','gemini-3.8-flash-low']
                self.p=subprocess.Popen(args,
                    cwd=self.root,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
            finally:os.close(slave)
            threading.Thread(target=self._read,daemon=True).start()
            self.wait(lambda s:'for shortcuts' in s or 'Do you trust the contents' in s,40)
            if 'Do you trust the contents' in self.display():self.write(b'\r')
            self.wait(lambda s:'Google AI' in s,45)
            self.wait(lambda s:'for shortcuts' in s and any(row.startswith('> ') for row in self.rows()),15)
        except BaseException:
            self.close();raise

    def _read(self):
        decoder=codecs.getincrementaldecoder('utf8')('replace')
        while not self.stop_event.is_set():
            try:
                if select.select([self.master],[],[],.2)[0]:
                    b=os.read(self.master,65536)
                    if not b:return
                    with self.lock:self.stream.feed(decoder.decode(b))
            except (OSError,ValueError):return

    def display(self):
        with self.lock:return '\n'.join(self.screen.display)

    def rows(self):
        with self.lock:return list(self.screen.display)

    def check(self):
        if self.cancel and self.cancel():raise RuntimeError("agy: request cancelled")
        if self.stop_event.is_set() or self.p.poll() is not None:raise RuntimeError('agy session closed')

    def write(self,data):
        self.check()
        with self.write_lock:
            view=memoryview(data)
            while view:
                n=os.write(self.master,view);view=view[n:]

    def wait(self,predicate,timeout):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.check()
            if predicate(self.display()):return
            time.sleep(.03)
        raise TimeoutError('agy: request timed out')

    def close(self):
        self.stop_event.set()
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:self.p.kill();self.p.wait(timeout=2)
        self.server.close()
        if self.master is not None:
            try:os.close(self.master)
            except OSError:pass
            self.master=None
        self.tmp.cleanup()
