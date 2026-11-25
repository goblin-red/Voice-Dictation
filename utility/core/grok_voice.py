"""Grok (xAI): диктовка потоком (wss /v1/stt) и озвучка ответов (POST /v1/tts).

Диктовка устроена как «Apple»: детектор речи (Listener) открывает фразу (start), досылает звук (feed)
и закрывает её (end); текст по ходу речи приходит в on_partial(id, text), окончательный — ждёт finish(id).
На каждую фразу — своё соединение (после audio.done сервер его закрывает); следующее открываем заранее,
чтобы начало фразы не ждало подключения (оно бывает до 3 с).
"""

import json
import os
import queue
import re
import subprocess

import audio_out
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

API = "https://api.x.ai/v1"
WSS = "wss://api.x.ai/v1/stt"
RATE = 16000
URL_MAX = 7000           # длина адреса потока: сервер принимает ~7500 (проверено), длиннее — HTTP 414; берём с запасом
SPARE_MAX_S = 25           # заранее открытое соединение старше — открываем новое
FINAL_WAIT_S = 4.0
VOICES = ("eve", "ara", "rex", "sal", "leo", "luna", "aurora", "celeste", "iris", "liora",
          "carina", "altair", "atlas", "castor", "cosmo", "helios", "helix", "kepler", "lumen", "lux")
END = object()             # метка «фраза кончилась» в очереди звука


GROK_AUTH = Path.home() / ".grok" / "auth.json"
_auth_lock = threading.Lock()
_last_access = None  # (выбранный режим, фактически использованный способ)


def api_key(cfg):
    """Доступ к голосу xAI (Bearer). grok_auth: subscription — Grok CLI sign-in (подписка SuperGrok),
    api — ключ API (grok_api_key или XAI_API_KEY). Подписка не работает — запасной путь: ключ API."""
    global _last_access
    key = (cfg.get("grok_api_key") or os.environ.get("XAI_API_KEY") or "").strip()
    mode = cfg.get("grok_auth", "subscription")
    if mode != "subscription":
        _last_access = (mode, "api" if key else None)
        return key
    try:
        token = subscription_token()
        _last_access = (mode, "subscription")
        return token
    except Exception:
        _last_access = (mode, "api" if key else None)
        if key:
            return key
        raise


def _cli_entry():
    data = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
    name, entry = next(iter(data.items()))
    return data, name, entry


def _expires(entry):
    from datetime import datetime
    return datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")).timestamp()


def subscription_token():
    """Токен входа Grok CLI (~/.grok/auth.json). Истекает через несколько часов — обновляем сами
    (refresh_token, OIDC) и записываем обратно в тот же файл: Grok CLI берёт уже новый."""
    with _auth_lock:
        if not GROK_AUTH.exists():
            raise RuntimeError("Grok CLI is signed out (~/.grok/auth.json) — run grok to sign in")
        data, name, entry = _cli_entry()
        if _expires(entry) - time.time() > 120:
            return entry["key"]
        issuer = entry.get("oidc_issuer") or "https://auth.x.ai"
        with urllib.request.urlopen(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=15) as r:
            token_url = json.loads(r.read())["token_endpoint"]
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                                       "client_id": entry.get("oidc_client_id", "")}).encode()
        req = urllib.request.Request(token_url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
        entry["key"] = tok["access_token"]
        if tok.get("refresh_token"):
            entry["refresh_token"] = tok["refresh_token"]
        from datetime import datetime, timezone
        entry["expires_at"] = datetime.fromtimestamp(time.time() + int(tok.get("expires_in", 3600)),
                                                     timezone.utc).isoformat().replace("+00:00", "Z")
        data[name] = entry
        tmp = GROK_AUTH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # токены — сразу 600, без окна 644
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(GROK_AUTH)
        return entry["key"]


def auth_info(cfg):
    """Для окна: чем сейчас ходим к xAI."""
    mode = cfg.get("grok_auth", "subscription")
    has_key = bool((cfg.get("grok_api_key") or os.environ.get("XAI_API_KEY") or "").strip())
    if mode != "subscription":
        return {"mode": "api", "effective_mode": "api" if has_key else None,
                "ok": has_key, "detail": "API key set" if has_key else "API key not set"}
    try:
        _, _, entry = _cli_entry()
        left = _expires(entry) - time.time()
        until = time.strftime("%H:%M", time.localtime(_expires(entry)))
        detail = (f"Grok CLI sign-in ({entry.get('email', '')}), token expires {until}" if left > 0
                  else f"Grok CLI sign-in ({entry.get('email', '')}), token refreshes on the first request")
        effective = _last_access[1] if _last_access and _last_access[0] == mode else "subscription"
        return {"mode": "subscription", "effective_mode": effective, "ok": True, "detail": detail}
    except Exception as e:
        return {"mode": "subscription", "effective_mode": "api" if has_key else None, "ok": False,
                "detail": "Grok CLI is signed out — run grok to sign in" + (" (using the API key for now)" if has_key else "")}


# ---------- диктовка

class _Phrase:
    def __init__(self, sid):
        self.sid = sid
        self.audio = queue.Queue()      # куски PCM (bytes) и END
        self.pcm = bytearray()          # весь звук фразы — на случай сбоя (распознать целиком по HTTP)
        self.done = threading.Event()
        self.utterances = []            # законченные высказывания (speech_final)
        self.chunks = []                # закреплённые куски после последнего высказывания
        self.interim = ""               # текст, который ещё может поменяться
        self.final = None               # окончательный текст
        self.failed = False

    def text(self):
        return " ".join(p for p in self.utterances + self.chunks + [self.interim] if p).strip()


class GrokSTT:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.on_partial = None          # функция (id, text) — текст по ходу речи
        self.error = None
        self._lang = "ru"
        self._hints = []
        self._seq = 0
        self._open = None               # фраза, в которую сейчас идёт звук
        self._phrases = {}
        self._spare = None              # (соединение, когда открыто)
        self._spare_lock = threading.Lock()
        self._active = False

    # ---------- настройка

    def ensure(self, lang, hints=()):
        """Включить: язык ru / en, слова команд — подсказка распознавателю. Проверяет ключ."""
        try:
            ok = api_key(self.cfg)
        except Exception as e:
            ok, self.error = "", str(e)
        if not ok:
            self.error = self.error or "xAI API key missing"
            raise RuntimeError(self.error)
        changed = lang != self._lang or list(hints) != self._hints
        self._lang = lang
        self._hints = [w for w in dict.fromkeys(hints) if w and len(w) <= 50][:100]
        self._active = True
        self.error = None
        if changed:
            self._drop_spare()
        threading.Thread(target=self._prepare, daemon=True).start()

    def stop(self):
        self._active = False
        ph, self._open = self._open, None
        if ph:
            ph.audio.put(END)            # фраза посреди речи: её поток допишет и закроет соединение, а не ждёт вечно
        self._drop_spare()

    def _url(self, limit=URL_MAX):
        q = [("model", "grok-voice-transcribe-2.0"), ("sample_rate", RATE), ("encoding", "pcm"),
             ("interim_results", "true"), ("language", self._lang)]
        if self.cfg.get("grok_format", True):
            q.append(("format", "true"))         # числа цифрами и т.п.
        url = WSS + "?" + urllib.parse.urlencode(q)
        # слова-подсказки — сколько влезет: слишком длинный адрес сервер отвергает (HTTP 414);
        # первыми идут слова Enter и прочих команд (порядок из настроек)
        for w in self._hints:
            more = "&" + urllib.parse.urlencode({"keyterm": w})
            if len(url) + len(more) > limit:
                break
            url += more
        return url

    def _connect(self):
        from websockets.sync.client import connect
        headers = {"Authorization": "Bearer " + api_key(self.cfg)}
        try:
            ws = connect(self._url(), additional_headers=headers, open_timeout=10, max_size=None)
        except Exception as e:
            if "414" not in str(e):
                raise
            # адрес всё же длинный для сервера — подсказок вдвое меньше
            self.log.warning("Grok: stream URL too long (HTTP 414) — reducing hints")
            ws = connect(self._url(URL_MAX // 2), additional_headers=headers, open_timeout=10, max_size=None)
        msg = json.loads(ws.recv(timeout=10))
        if msg.get("type") != "transcript.created":
            ws.close()
            raise RuntimeError(msg.get("message") or str(msg)[:200])
        return ws

    def _prepare(self):
        """Открыть соединение заранее — для следующей фразы."""
        with self._spare_lock:
            if not self._active or (self._spare and self._fresh(self._spare)):
                return
            self._close(self._spare)
            self._spare = None
            try:
                self._spare = (self._connect(), time.time())
            except Exception as e:
                self.error = str(e)
                self.log.warning("Grok: connection failed: %s", e)

    def _take(self):
        """Соединение для новой фразы: заранее открытое, если живо, иначе новое."""
        with self._spare_lock:
            spare, self._spare = self._spare, None
        if spare and self._fresh(spare):
            return spare[0]
        self._close(spare)
        return self._connect()

    @staticmethod
    def _fresh(spare):
        from websockets.protocol import State
        ws, at = spare
        return ws.protocol.state is State.OPEN and time.time() - at < SPARE_MAX_S

    @staticmethod
    def _close(spare):
        if spare:
            try:
                spare[0].close()
            except Exception:
                pass

    def _drop_spare(self):
        with self._spare_lock:
            self._close(self._spare)
            self._spare = None

    # ---------- фразы (вызывает детектор речи)

    def start(self, samples):
        """Новая фраза; samples — звук от её начала (float32). Возвращает id или None."""
        if not self._active:
            return None
        self._seq += 1
        ph = _Phrase(f"g{self._seq}")
        self._phrases[ph.sid] = ph
        self._open = ph
        self.feed(samples)
        threading.Thread(target=self._run, args=(ph,), daemon=True).start()
        return ph.sid

    def feed(self, samples):
        ph = self._open
        if ph:
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            ph.pcm += pcm
            ph.audio.put(pcm)

    def end(self):
        ph, self._open = self._open, None
        if ph:
            ph.audio.put(END)
            return ph.sid
        return None

    def discard(self, sid):
        ph = self._phrases.pop(sid, None)
        if ph:
            ph.audio.put(END)

    def finish(self, sid, timeout=FINAL_WAIT_S):
        """Окончательный текст фразы (ждём до timeout после её конца)."""
        ph = self._phrases.get(sid)
        if not ph:
            return ""
        if not ph.done.wait(timeout):
            self.log.warning("Grok: no final transcript — using the interim transcript")
        self._phrases.pop(sid, None)
        if ph.final is not None:
            return ph.final
        if ph.failed and not ph.text():
            return self.transcribe_pcm(bytes(ph.pcm))     # поток сорвался — целиком по HTTP
        return ph.text()

    # ---------- соединение фразы

    def _run(self, ph):
        try:
            ws = self._take()
        except Exception as e:
            self.error = str(e)
            self.log.warning("Grok: %s", e)
            ph.failed = True
            ph.done.set()
            return
        threading.Thread(target=self._prepare, daemon=True).start()   # следующей фразе — новое соединение
        reader = threading.Thread(target=self._read, args=(ws, ph), daemon=True)
        reader.start()
        try:
            while True:
                chunk = ph.audio.get()
                if chunk is END:
                    break
                ws.send(chunk)
            ws.send(json.dumps({"type": "audio.done"}))
        except Exception as e:
            self.log.warning("Grok: could not send audio: %s", e)
            ph.failed = True
            ws.close()
        reader.join(15)
        ws.close()
        ph.done.set()

    def _read(self, ws, ph):
        try:
            for raw in ws:
                ev = json.loads(raw)
                kind = ev.get("type")
                if kind == "transcript.partial":
                    text = (ev.get("text") or "").strip()
                    if ev.get("speech_final"):
                        ph.utterances.append(text)      # высказывание целиком — вместо его кусков
                        ph.chunks, ph.interim = [], ""
                    elif ev.get("is_final"):
                        ph.chunks.append(text)
                        ph.interim = ""
                    else:
                        ph.interim = text
                    if self.on_partial and ph.sid in self._phrases:
                        self.on_partial(ph.sid, ph.text())
                elif kind == "transcript.done":
                    ph.final = ph.text()
                    break
                elif kind == "error":
                    self.error = ev.get("message")
                    self.log.warning("Grok: %s", self.error)
                    ph.failed = True
        except Exception as e:
            if ph.final is None:
                self.log.warning("Grok: disconnected: %s", e)
                ph.failed = True
        finally:
            ph.done.set()

    # ---------- целиком (запасной путь и фразы без потока)

    def transcribe_pcm(self, pcm):
        """Сырой звук (int16, 16 кГц) -> текст по HTTP."""
        if not pcm:
            return ""
        # язык — из настроек Voice (language: язык окна или auto), а не прошлый язык потока: в диктовке ZIPA
        # поток Grok не запускается, и здесь оставался «ru» по умолчанию; auto — язык не передаём
        lang = self.cfg.get("language") or self._lang
        fields = [("model", "grok-voice-transcribe-2.0"), ("audio_format", "pcm"), ("sample_rate", str(RATE))] \
            + ([("language", lang)] if lang != "auto" else []) + [("keyterm", w) for w in self._hints]
        if self.cfg.get("grok_format", True):
            fields.append(("format", "true"))
        boundary = "grok" + str(time.time_ns())
        body = bytearray()
        for name, value in fields:          # поля — до файла (после файла сервер их не читает)
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.pcm"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode() + pcm + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API + "/stt", data=bytes(body), method="POST", headers={
            "Authorization": "Bearer " + api_key(self.cfg), "Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (json.loads(resp.read()).get("text") or "").strip()

    def transcribe(self, wav):
        """WAV (16 кГц, моно, 16 бит) -> текст."""
        return self.transcribe_pcm(wav[44:])


# ---------- озвучка

class GrokTTS:
    """Озвучка голосом Grok: текст режем по предложениям, следующий кусок готовим, пока звучит прошлый."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._token = 0
        self._playing = False
        self._proc = None

    def voice(self, name=None):
        return (name or self.cfg.get("grok_voice") or "eve").lower()

    def speak(self, text, voice=None, rate=190):
        """Озвучить в фоне; возвращает сразу."""
        self._token += 1
        token = self._token
        self._kill()
        self._playing = True
        threading.Thread(target=self._speak, args=(text, self.voice(voice), rate, token), daemon=True).start()

    def _speak(self, text, voice, rate, token):
        speed = max(0.7, min(1.5, rate / 190))          # 190 слов/мин в Orca = обычный темп
        lang = "ru" if re.search(r"[а-яё]", text, re.I) else "en"
        ready = queue.Queue()

        def fetch():
            for chunk in self._chunks(text):
                if token != self._token:
                    break
                try:
                    ready.put(self._synth(chunk, voice, lang, speed))
                except Exception as e:
                    self.log.warning("Grok speech: %s", e)
                    break
            ready.put(None)

        threading.Thread(target=fetch, daemon=True).start()
        try:
            while token == self._token:
                audio = ready.get()
                if audio is None or token != self._token:
                    break
                self._play(audio, token)
        finally:
            if token == self._token:
                self._playing = False

    def _synth(self, text, voice, lang, speed, bit_rate=128000):
        body = json.dumps({"text": text, "voice_id": voice, "language": lang, "speed": speed,
                           "output_format": {"codec": "mp3", "sample_rate": 24000, "bit_rate": bit_rate}}).encode()
        req = urllib.request.Request(API + "/tts", data=body, method="POST", headers={
            "Authorization": "Bearer " + api_key(self.cfg), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    def render(self, text, voice=None, rate=190):
        """MP3 по кускам (по предложениям) — для отправки на телефон."""
        speed = max(0.7, min(1.5, rate / 190))
        lang = "ru" if re.search(r"[а-яё]", text, re.I) else "en"
        for chunk in self._chunks(text):
            yield self._synth(chunk, self.voice(voice), lang, speed, bit_rate=64000)

    @staticmethod
    def _chunks(text, first=200, limit=1500):
        """Первый кусок короткий (быстрее зазвучит), дальше — крупнее."""
        out, cur = [], ""
        for sentence in re.split(r"(?<=[.!?…])\s+", text):
            cap = first if not out else limit
            if cur and len(cur) + len(sentence) > cap:
                out.append(cur)
                cur = ""
            cur = (cur + " " + sentence).strip()
        if cur:
            out.append(cur)
        return out

    def _play(self, audio, token):
        """Кусок MP3 -> временный файл -> afplay (играет на текущем выходе macOS)."""
        fd, path = tempfile.mkstemp(suffix=".mp3")
        try:
            os.write(fd, audio)
            os.close(fd)
            if token != self._token:
                return
            self._proc = audio_out.play(path)          # выбранный выход или как в macOS
            self._proc.wait()
        finally:
            os.unlink(path)

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None

    def stop(self):
        self._token += 1
        self._playing = False
        self._kill()

    def busy(self):
        return self._playing
