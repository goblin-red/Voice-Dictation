"""Direct text requests in persistent agy Live; incremental subscription audio.
The user permits commentary and paraphrases. No macOS synthesis or ASR gating.
"""
import io, os, queue, select, shutil, signal, subprocess, sys, tempfile, threading, time, wave
from pathlib import Path
import audio_out
from grok_voice import GrokTTS
from agy_live_reader import LiveReader


def wav_bytes(pcm,rate):
    b=io.BytesIO()
    with wave.open(b,'wb') as w:w.setparams((1,2,rate,0,'NONE',''));w.writeframes(pcm)
    return b.getvalue()


class GeminiTTS(GrokTTS):
    def __init__(self,cfg,log,verify=None):
        super().__init__(cfg,log)
        self._session=None;self._serial=threading.Lock();self._generating=False
        self.last_stats={}
        self._play_lock=threading.Lock();self._stream_proc=None

    def voice(self,name=None):return 'agy'

    def _kill(self):
        with self._play_lock:
            p,self._stream_proc=self._stream_proc,None
            if p:self._terminate_stream(p)
        super()._kill()
        if self._generating:
            s,self._session=self._session,None
            if s:s.close()

    def close(self):
        self.stop()
        s,self._session=self._session,None
        if s:s.close()

    def warm(self):
        token=self._token
        def run():
            try:
                with self._serial:self._ensure(token)
            except Exception as e:self.log.warning('Gemini connection: %s',e)
        threading.Thread(target=run,daemon=True).start()

    def _ensure(self,token):
        self._check(token)
        if self._session and (self._session.p.poll() is not None or self._session.fault):
            self._session.close();self._session=None
        if not self._session:
            s=LiveReader(cancel=lambda:token!=self._token)
            if token!=self._token:s.close();raise RuntimeError('Gemini: cancelled')
            self._session=s
        return self._session

    def _check(self,token):
        if token!=self._token:raise RuntimeError('Gemini: speech stopped')

    @staticmethod
    def _mp3(pcm,speed):
        p=subprocess.run([shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg','-v','error',
            '-f','s16le','-ar','24000','-ac','1','-i','pipe:0','-filter:a',f'atempo={speed}',
            '-f','mp3','-b:a','64k','pipe:1'],input=pcm,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,check=True)
        return p.stdout

    def render(self,text,voice=None,rate=190,*,pcm=False):
        token=self._token;self._playing=True
        q=queue.Queue(maxsize=8);cancel=threading.Event()
        def put(item):
            while not cancel.is_set() and token==self._token:
                try:q.put(item,timeout=.1);return
                except queue.Full:pass
        def produce():
            try:
                with self._serial:
                    self._generating=True
                    try:
                        self._check(token)
                        if getattr(self, 'on_prepare', None):
                            try:
                                self.on_prepare()
                            except Exception as exc:
                                self.log.warning('Gemini: could not play the preparation cue: %s', exc)
                        s=self._ensure(token)
                        for block in s.stream_text(str(text),lambda:self._check(token)):
                            self._check(token)
                            put(block if pcm else self._mp3(block,max(.7,min(1.5,rate/190))))
                        self.last_stats=s.last_stats
                        self.log.info('Gemini Live: first audio %.2f s, request %.2f s',s.last_stats['first_audio'],s.last_stats['total'])
                    except Exception:
                        s,self._session=self._session,None
                        if s:s.close()
                        raise
                    finally:self._generating=False
            except Exception as e:put(e)
            finally:put(None)
        worker=threading.Thread(target=produce,daemon=True);worker.start()
        finished=False
        try:
            while token==self._token:
                try:item=q.get(timeout=.1)
                except queue.Empty:continue
                if item is None:
                    finished=True
                    return
                if isinstance(item,Exception):raise item
                yield item
        finally:
            cancel.set()
            if token==self._token:
                if not pcm:self._playing=False
                if not finished and worker.is_alive():self.stop()

    def speak(self,text,voice=None,rate=190):
        self.stop();token=self._token;self._playing=True
        def run():
            try:
                if token!=self._token:return
                self._play_continuous(text,rate,token)
            except Exception as e:
                if token==self._token:
                    self.log.warning('Gemini speech: %s',e)
                    if getattr(self,'on_error',None):self.on_error(str(e))
            finally:
                if token==self._token:self._playing=False
        threading.Thread(target=run,daemon=True).start()

    @staticmethod
    def _terminate_stream(p):
        # The helper and its tempo filter share a private process group.
        try:os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        p.wait(timeout=3)

    def _play_continuous(self,text,rate,token):
        source=self.render(text,rate=rate,pcm=True)
        audio=tempfile.SpooledTemporaryFile(max_size=2*1024*1024)
        p=None
        try:
            mode = self.cfg.get('gemini_playback', 'complete')
            self.log.info('Gemini: playback mode %s', mode)
            if mode == 'complete':
                for block in source:
                    self._check(token)
                    audio.write(block)
                self._check(token)
                self.log.info('Gemini: received speech, %.2f s of audio', audio.tell()/48000)
                audio.seek(0)
                blocks = iter(lambda: audio.read(48000), b'')
            else:
                blocks = source
            for block in blocks:
                self._check(token)
                if p is None:
                    with self._play_lock:
                        self._check(token)
                        p=subprocess.Popen([sys.executable,'-I',str(Path(__file__).with_name('pcm_play.py')),
                            audio_out.chosen('out') or '',str(audio_out.output_key()[0]),
                            str(max(.7,min(1.5,rate/190)))],stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True,bufsize=0)
                        self._stream_proc=p
                        os.set_blocking(p.stdin.fileno(),False)
                pending=memoryview(block);deadline=time.monotonic()+15
                while pending:
                    self._check(token)
                    if p.poll() is not None:raise RuntimeError('Gemini: playback stream stopped with an error')
                    if time.monotonic()>deadline:raise TimeoutError('Gemini: audio output stalled')
                    if select.select([],[p.stdin],[],.1)[1]:
                        try:n=os.write(p.stdin.fileno(),pending)
                        except BlockingIOError:continue
                        pending=pending[n:];deadline=time.monotonic()+15
            if p:
                p.stdin.close()
                deadline=time.monotonic()+15
                while p.poll() is None:
                    self._check(token)
                    if time.monotonic()>deadline:raise TimeoutError('Gemini: audio output did not finish')
                    time.sleep(.05)
                diagnostics = p.stderr.read(2000).decode(errors='replace').strip()
                if p.returncode:
                    raise RuntimeError('Gemini: '+diagnostics)
                if diagnostics:
                    self.log.info('Gemini playback: %s', diagnostics)
        finally:
            source.close()
            audio.close()
            if p:
                with self._play_lock:
                    if self._stream_proc is p:self._stream_proc=None
                    self._terminate_stream(p)
                p.stdin.close();p.stderr.close()
