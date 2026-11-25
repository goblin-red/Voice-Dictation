"""Streaming subscription dictation with a persistent, isolated agy session."""
import io, queue, threading, time, wave
from types import SimpleNamespace
import numpy as np
from agy_session import AgySession

END=object()

class GeminiSTT:
    def __init__(self,log):
        self.log=log;self.on_partial=lambda sid,text:None
        self._session=None;self._serial=threading.Lock();self._epoch=0
        self._active=False;self._seq=0;self._open=None;self._phrases={};self.error=None

    def _ensure(self,epoch):
        if epoch!=self._epoch:raise RuntimeError('Gemini: request cancelled')
        if self._session and self._session.p.poll() is not None:
            self._session.close();self._session=None
        if not self._session:
            s=AgySession(cancel=lambda:epoch!=self._epoch)
            s.cancel=None
            if epoch!=self._epoch:s.close();raise RuntimeError('Gemini: request cancelled')
            s.placeholder=self.prompt(s.rows())
            self._session=s
        self.error=None
        return self._session

    def ensure(self,lang='ru',hints=()):
        self._active=True;epoch=self._epoch
        def warm():
            try:
                with self._serial:self._ensure(epoch)
            except Exception as e:self.error=str(e)
        threading.Thread(target=warm,daemon=True).start()

    def stop(self):
        self._active=False;self._epoch+=1
        s,self._session=self._session,None
        if s:s.close()
        for ph in list(self._phrases.values()):ph.audio.put(END)
        self._open=None
        self._phrases.clear()

    def start(self,samples):
        if not self._active:return None
        self._seq+=1;sid='gemini'+str(self._seq)
        ph=SimpleNamespace(sid=sid,audio=queue.Queue(),done=threading.Event(),text='',error=None,epoch=self._epoch)
        self._phrases[sid]=ph;self._open=ph;self.feed(samples)
        threading.Thread(target=self._run,args=(ph,),daemon=True).start()
        return sid

    def feed(self,samples):
        if self._open:self._open.audio.put((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())

    def end(self):
        ph,self._open=self._open,None
        if ph:ph.audio.put(END);return ph.sid

    def discard(self,sid):
        ph=self._phrases.pop(sid,None)
        if ph:ph.audio.put(END)

    def finish(self,sid,timeout=15):
        ph=self._phrases.get(sid)
        if not ph:return ''
        if not ph.done.wait(max(timeout,15)):
            self.stop();raise TimeoutError('Gemini: no final transcript')
        self._phrases.pop(sid,None)
        if ph.error:raise RuntimeError(ph.error)
        return ph.text

    def _run(self,ph):
        with self._serial:self._run_locked(ph)

    def _run_locked(self,ph):
        s=None
        try:
            s=self._ensure(ph.epoch)
            s.write(b'\x15')
            s.wait(lambda _:self.prompt(s.rows()) in ('',None,s.placeholder) or (self.prompt(s.rows()) or '').startswith('Plan mode:'),3)
            s.write(b'/voice dictate\r')
            deadline=time.monotonic()+25;c=None
            while time.monotonic()<deadline:
                s.check()
                if 'another session on this account is already using voice input' in s.display():
                    raise RuntimeError('Gemini: dictation is in use by another agy session on this account')
                try:c,_=s.server.accept();break
                except TimeoutError:pass
            if c is None:raise TimeoutError('Gemini: microphone stream is not open')
            with c:
                c.settimeout(3);s.wait(lambda d:'Recording' in d,15)
                while True:
                    if ph.epoch!=self._epoch:raise RuntimeError('Gemini: phrase cancelled')
                    try:data=ph.audio.get(timeout=.05)
                    except queue.Empty:data=b''
                    if data is END:break
                    if data:c.sendall(data)
                    text=self.prompt(s.rows()) or ''
                    if text and text!=s.placeholder and text!=ph.text and not text.startswith(('/voice','Plan mode:')):
                        ph.text=text;self.on_partial(ph.sid,text)
                s.write(b'\r')
                import socket
                c.shutdown(socket.SHUT_WR)
            s.wait(lambda d:'Recording' not in d and 'Finishing' not in d and 'for shortcuts' in d,15)
            ph.text=self.prompt(s.rows()) or ''
            if ph.text.startswith(('/voice','Plan mode:')):ph.text=''
            s.write(b'\x15')
        except Exception as e:
            ph.error=str(e)
            # A cancelled phrase from the previous mode must not close a new session.
            if ph.epoch==self._epoch:self.error=str(e)
            if s is not None:
                if self._session is s:self._session=None
                s.close()
        finally:ph.done.set()

    def transcribe(self,wav):
        with wave.open(io.BytesIO(wav),'rb') as w:
            if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,16000):raise ValueError('PCM16 mono at 16 kHz is required')
            pcm=w.readframes(w.getnframes())
        self._active=True
        sid=self.start(np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768)
        self.end();return self.finish(sid,90)
    @staticmethod
    def prompt(rows):
        """Read only the editable prompt bounded by TUI horizontal rules."""
        for i in range(len(rows) - 1, -1, -1):
            row = rows[i]
            if row.startswith('> ') and i > 0 and rows[i - 1].lstrip().startswith(('─', '━')):
                lines = [row[2:].rstrip()]
                for row in rows[i + 1:]:
                    if row.lstrip().startswith(('─', '━')):
                        return '\n'.join(lines).strip()
                    lines.append(row.rstrip())
        return None
