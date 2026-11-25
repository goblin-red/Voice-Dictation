"""OpenAI по ключу API: диктовка потоком (Realtime, модель gpt-live-transcribe) и озвучка (gpt-4o-mini-tts).

Устроено как grok_voice: те же классы фраз и озвучки, свой протокол. По подписке (вход Codex)
OpenAI работает через live.py — этот модуль только для режима «Ключ API» (openai_auth = api).
"""

import base64
import io
import json
import os
import time
import urllib.request
import wave

import numpy as np

from grok_voice import RATE, GrokSTT, GrokTTS, END

API = "https://api.openai.com/v1"
WSS = "wss://api.openai.com/v1/realtime?intent=transcription"
OUT_RATE = 24000                # Realtime принимает PCM 24 кГц
STT_MODEL = "gpt-live-transcribe"
TTS_MODEL = "gpt-4o-mini-tts"
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse")


def api_key(cfg):
    """Ключ OpenAI API: из настроек, иначе из переменной окружения OPENAI_API_KEY."""
    return (cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def api_mode(cfg):
    """Выбран «Ключ API» и ключ задан."""
    return cfg.get("openai_auth", "subscription") == "api" and bool(api_key(cfg))


def _to24k(pcm16_bytes):
    """int16 16 кГц -> int16 24 кГц."""
    x = np.frombuffer(pcm16_bytes, dtype="<i2").astype(np.float32)
    if not len(x):
        return b""
    idx = np.arange(0, len(x), RATE / OUT_RATE)
    return np.interp(idx, np.arange(len(x)), x).astype("<i2").tobytes()


class OpenAISTT(GrokSTT):
    """Фраза = соединение: звук (base64, 24 кГц) → commit → текст по ходу (delta) и окончательный (completed)."""

    def ensure(self, lang, hints=()):
        """Включить: язык ru / en, слова команд — подсказка. Нужен ключ OpenAI API."""
        if not api_key(self.cfg):
            self.error = "OpenAI API key missing"
            raise RuntimeError(self.error)
        hints = [w for w in dict.fromkeys(hints) if w][:100]
        changed = lang != self._lang or hints != self._hints
        self._lang, self._hints = lang, hints
        self._active = True
        self.error = None
        if changed:
            self._drop_spare()
        import threading
        threading.Thread(target=self._prepare, daemon=True).start()

    def _connect(self):
        from websockets.sync.client import connect
        ws = connect(WSS, additional_headers={"Authorization": "Bearer " + api_key(self.cfg)},
                     open_timeout=10, max_size=None)
        msg = json.loads(ws.recv(timeout=10))
        if msg.get("type") != "session.created":
            ws.close()
            raise RuntimeError((msg.get("error") or {}).get("message") or str(msg)[:200])
        transcription = {"model": STT_MODEL, "language": self._lang}
        if self._hints:
            transcription["prompt"] = ", ".join(self._hints)     # слова команд — подсказка
        ws.send(json.dumps({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": OUT_RATE}, "transcription": transcription,
            "turn_detection": None}}}}))
        return ws

    def _run(self, ph):
        try:
            ws = self._take()
        except Exception as e:
            self.error = str(e)
            self.log.warning("OpenAI: %s", e)
            ph.failed = True
            ph.done.set()
            return
        import threading
        threading.Thread(target=self._prepare, daemon=True).start()
        reader = threading.Thread(target=self._read, args=(ws, ph), daemon=True)
        reader.start()
        try:
            while True:
                chunk = ph.audio.get()
                if chunk is END:
                    break
                ws.send(json.dumps({"type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(_to24k(chunk)).decode()}))
            ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception as e:
            self.log.warning("OpenAI: could not send audio: %s", e)
            ph.failed = True
            ws.close()
        reader.join(15)
        ws.close()
        ph.done.set()

    def _read(self, ws, ph):
        try:
            for raw in ws:
                ev = json.loads(raw)
                kind = ev.get("type", "")
                if kind.endswith("input_audio_transcription.delta"):
                    ph.interim += ev.get("delta") or ""
                    if self.on_partial and ph.sid in self._phrases:
                        self.on_partial(ph.sid, ph.interim.strip())
                elif kind.endswith("input_audio_transcription.completed"):
                    ph.final = (ev.get("transcript") or "").strip()
                    break
                elif kind == "error":
                    err = ev.get("error") or {}
                    if err.get("code") == "input_audio_buffer_commit_empty":
                        ph.final = ""            # фраза без звука
                        break
                    self.error = err.get("message") or str(ev)[:200]
                    self.log.warning("OpenAI: %s", self.error)
                    ph.failed = True
                    break
        except Exception as e:
            if ph.final is None:
                self.log.warning("OpenAI connection dropped: %s", e)
                ph.failed = True
        finally:
            ph.done.set()

    def transcribe_pcm(self, pcm):
        """Сырой звук (int16, 16 кГц) -> текст по HTTP (запасной путь)."""
        if not pcm:
            return ""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        boundary = "oa" + str(time.time_ns())
        fields = [("model", "gpt-4o-transcribe"), ("language", self._lang)]
        if self._hints:
            fields.append(("prompt", ", ".join(self._hints)))
        body = bytearray()
        for name, value in fields:
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n').encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API + "/audio/transcriptions", data=bytes(body), method="POST", headers={
            "Authorization": "Bearer " + api_key(self.cfg), "Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (json.loads(resp.read()).get("text") or "").strip()


class OpenAITTS(GrokTTS):
    """Озвучка голосом OpenAI (gpt-4o-mini-tts) — так же, как Grok: по предложениям, MP3, afplay."""

    def _speak(self, text, voice, rate, token):
        if self.cfg.get("openai_playback", "stream") != "complete":
            return super()._speak(text, voice, rate, token)
        try:
            audio = bytearray()
            for block in super().render(text, voice, rate):
                if token != self._token:
                    return
                audio.extend(block)
            if audio and token == self._token:
                self._play(bytes(audio), token)
        except Exception as exc:
            self.log.warning("OpenAI speech: %s", exc)
        finally:
            if token == self._token:
                self._playing = False

    def voice(self, name=None):
        name = (name or self.cfg.get("openai_voice") or "marin").lower()
        return name if name in VOICES else "marin"

    def _synth(self, text, voice, lang, speed, bit_rate=128000):
        body = json.dumps({"model": TTS_MODEL, "voice": voice, "input": text, "speed": speed,
                           "response_format": "mp3"}).encode()
        req = urllib.request.Request(API + "/audio/speech", data=body, method="POST", headers={
            "Authorization": "Bearer " + api_key(self.cfg), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
