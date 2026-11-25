"""OpenAI live (gpt-live-1-codex) через подписку ChatGPT — по схеме jauvex.

Звук микрофона уходит по WebRTC, распознанный текст приходит по WebSocket (sideband).
Доступ — вход Codex (~/.codex/auth.json), API-ключ не нужен.

Озвучка голосом OpenAI (настройка tts = openai): текст ответа агента уходит модели
в канал «speakable», модель читает его вслух, звук приходит по WebRTC и играет в динамики.
"""

import asyncio
import fractions
import json
import queue
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import sounddevice as sd

import audio_out
import websockets
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

from i18n import t

MODEL = "gpt-live-1-codex"
CALL_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
SIDEBAND_URL = "wss://api.openai.com/v1/live/"
PROMPT = (
    "You are a transparent voice relay between the user and a desktop coding agent. "
    "Listen silently while the user speaks. As soon as the user pauses and the phrase is complete, "
    "immediately delegate it once, verbatim, without waiting for more speech. "
    "Never repeat or answer the user aloud. The app sends the final transcript directly to its "
    "selected coding agent. When the app gives you an agent reply, speak only that reply, "
    "in full and verbatim. Do not add commentary."
)

# язык речи пользователя: дописывается к инструкции, чтобы OpenAI не «уплывал» в другие языки
LANG_RULES = {
    "ru": ("The user speaks Russian. Always transcribe the user's speech in Russian, written in Cyrillic, "
           "even for short, noisy or unclear phrases. Never transcribe it as Spanish, Italian, English "
           "or any other language."),
    "en": ("The user speaks English. Always transcribe the user's speech in English, "
           "even for short, noisy or unclear phrases. Never transcribe it as any other language."),
}


def instructions(lang, prompt=""):
    """Инструкция сессии: общая часть (своя из настроек live_prompt или PROMPT) + правило языка (для auto — без правила)."""
    rule = LANG_RULES.get(lang)
    return ((prompt or "").strip() or PROMPT) + (" " + rule if rule else "")


RATE = 16000
CHUNK = 320   # 20 мс при 16 кГц
FLUSH_S = 0.9 # пауза в кусочках текста, после которой фраза считается законченной
SPEAK_CHUNK = 500   # байт текста в одном сообщении «прочитай вслух»
LOUD = 300          # уровень (int16), выше которого звук считаем речью, а не тишиной


# ---------- доступ

def credentials():
    """Токен и аккаунт из входа Codex через ChatGPT."""
    path = Path.home() / ".codex" / "auth.json"
    tokens = json.loads(path.read_text(encoding="utf-8")).get("tokens") or {}
    if not tokens.get("access_token") or not tokens.get("account_id"):
        raise RuntimeError(t("err.no_codex_login"))
    return tokens["access_token"], tokens["account_id"]


def make_headers(token, account):
    return {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-ID": account,
        "OpenAI-Alpha": "quicksilver=v2",
        "session-id": str(uuid.uuid4()),
        "thread-id": str(uuid.uuid4()),
        "x-session-id": str(uuid.uuid4()),
        "originator": "orca-voice",
        "User-Agent": "orca-voice/1.0",
    }


# ---------- микрофон -> WebRTC

class MicTrack(MediaStreamTrack):
    """Аудиодорожка WebRTC из кадров, которые приходят из Listener (float32, 16 кГц)."""

    kind = "audio"

    def __init__(self, loop, session):
        super().__init__()
        self.loop = loop
        self.session = session
        self.queue = asyncio.Queue()
        self.buffer = np.zeros(0, dtype=np.float32)
        self.pts = 0

    def feed(self, samples):
        """Вызывается из потока микрофона."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, samples)

    async def recv(self):
        if not self.session.listen:
            # OpenAI только читает ответы: микрофон ему не нужен — тишина в своём темпе
            await asyncio.sleep(CHUNK / RATE)
            self.buffer = np.zeros(0, dtype=np.float32)
            chunk = np.zeros(CHUNK, dtype=np.float32)
        else:
            while len(self.buffer) < CHUNK:
                self.buffer = np.concatenate([self.buffer, await self.queue.get()])
            chunk, self.buffer = self.buffer[:CHUNK], self.buffer[CHUNK:]

        pcm = (np.clip(chunk, -1, 1) * 32767).astype(np.int16).reshape(1, -1)
        frame = AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, RATE)
        self.pts += CHUNK
        return frame


# ---------- голос OpenAI -> динамики

class AudioOut:
    """Проигрывает звук от OpenAI в динамики. Пишет в своём потоке, чтобы не тормозить asyncio."""

    def __init__(self, log):
        self.log = log
        self.last_loud = 0.0        # когда последний раз звучала речь (не тишина)
        self._queue = queue.Queue()
        self._stream = None
        self._format = None
        self.sink = None            # (pcm, rate, channels): звук не в динамики, а туда (озвучка на телефон)
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, pcm, rate, channels):
        sink = self.sink
        if sink:
            if int(np.abs(pcm).max(initial=0)) > LOUD:
                self.last_loud = time.time()
            sink(pcm, rate, channels)
            return
        self._queue.put((pcm, rate, channels))

    def clear(self):
        """Выбросить всё, что ещё не проиграно (команда «замолчать»)."""
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                return

    def close(self):
        self.clear()
        self._queue.put(None)

    def _open(self, rate, channels):
        with audio_out.pa_lock():           # в общей очереди к PortAudio (иначе CoreAudio может зависнуть)
            old, self._stream = self._stream, None
            if old:
                try:
                    old.close()
                except Exception:
                    pass                    # PortAudio перезапускали — старый поток мёртв, закрывать нечего
            name = audio_out.chosen("out")
            device = audio_out.index(name, "out")
            if not audio_out.output_available() or (audio_out.output_key()[0] and device is None):
                raise RuntimeError("The selected output is unavailable")
            self._stream = sd.OutputStream(samplerate=rate, channels=channels, dtype="int16", device=device)
            self._stream.start()
        self._format = (rate, channels, audio_out.output_key())

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            pcm, rate, channels = item
            try:
                if self._format != (rate, channels, audio_out.output_key()) or not audio_out.output_available():
                    self._open(rate, channels)
                if int(np.abs(pcm).max(initial=0)) > LOUD:
                    self.last_loud = time.time()
                self._stream.write(pcm)
            except Exception as e:
                self.log.warning("live audio: %s", e)
                self._format = None          # следующий кусок — в заново открытый поток (устройство могли сменить)
            finally:
                self._queue.task_done()
        if self._stream:
            with audio_out.pa_lock():
                try:
                    self._stream.close()
                except Exception:
                    pass


# ---------- разбор событий (как parseLiveMessage / LiveTranscriptBuffer в jauvex)

def parse_event(raw):
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    kind = event.get("type")

    if kind == "input_transcript.added" and isinstance((event.get("item") or {}).get("text"), str):
        return ("fragment", event["item"]["text"])
    if kind == "session.input_transcript.delta" and isinstance(event.get("delta"), str):
        return ("fragment", event["delta"])
    if kind == "turn.done":
        turn = event.get("turn") or {}
        if turn.get("role") == "user" and (turn.get("transcript") or "").strip():
            return ("transcript", turn["transcript"])
        if turn.get("role") == "assistant":
            return ("spoken", None)
    if kind in ("output_transcript.added", "session.output_transcript.delta"):
        return ("speaking", None)
    if kind == "delegation.created" and (event.get("item") or {}).get("id"):
        return ("delegation", event["item"]["id"])
    if kind == "error":
        return ("error", (event.get("error") or {}).get("message") or t("err.live"))
    return None


def _norm(text):
    return re.sub(r"[^\w]+", "", text.lower())


class TranscriptBuffer:
    """Кусочки текста -> фразы; итоговый текст хода не дублирует уже отданные фразы.

    OpenAI делегирует только первую фразу сессии, дальше присылает лишь кусочки текста —
    поэтому фразу закрываем сами, по паузе в кусочках (FLUSH_S).
    """

    def __init__(self):
        self.partial = ""
        self.early = []
        self.last = 0.0      # когда пришёл последний кусочек

    def add(self, fragment):
        self.partial += fragment
        self.last = time.monotonic()

    def flush(self):
        text, self.partial = self.partial, ""
        if not text.strip():
            return None
        self.early = (self.early + [text])[-50:]
        return text

    def due(self):
        """Пора закрывать фразу: есть текст и кусочки давно не приходили."""
        return bool(self.partial.strip()) and time.monotonic() - self.last > FLUSH_S

    def on_delegation(self):
        return self.flush()

    def on_final(self, text):
        final = _norm(text)
        for count in range(len(self.early), 0, -1):
            for start in range(len(self.early) - count + 1):
                if _norm("".join(self.early[start:start + count])) == final:
                    del self.early[start:start + count]
                    return None
        self.partial = ""
        return text


# ---------- сессия

class LiveSession:
    """Одна live-сессия в своём потоке с asyncio.

    on_text(text)   — распознанная фраза
    on_state(state, detail) — "connecting" / "live" / "closed" / "error"
    """

    def __init__(self, on_text, on_state, log, lang=None, prompt=""):
        self.prompt = prompt     # своя инструкция из настроек (пусто — PROMPT)
        self.on_text = on_text
        # распознанные фразы печатаются в своём потоке по очереди: печать (orca, osascript, паузы перед Enter)
        # не должна стоять в цикле asyncio — он же крутит звук WebRTC и WebSocket (иначе заикание и обрывы)
        self._texts = queue.Queue()
        threading.Thread(target=self._text_loop, daemon=True).start()
        self.on_state = on_state
        self.log = log
        self.loop = None
        self.track = None
        self._stop = None
        self._cancel = False     # stop() мог прийти раньше, чем поднялся asyncio
        self._thread = None
        self.ws = None
        self.lang = lang         # ru / en / auto — язык распознавания (инструкция сессии)
        self.listen = True       # False — сессия только для голоса: микрофон в OpenAI не идёт

        # озвучка голосом OpenAI
        self.audio = AudioOut(log)
        self._audio_on = False   # пускать звук модели в динамики (только когда сами попросили читать)
        self._speaking = False   # модель читает наш текст
        self._said_speaking, self._frames_heard = True, 0   # подробный журнал озвучки (speak() сбрасывает)
        self._deadline = 0.0     # запасной конец чтения, если turn.done не придёт
        self.playback_mode = lambda: "stream"
        self._playback_lock = threading.RLock()
        self._playback_serial = 0
        self._buffered_audio = []
        self._buffered_seconds = 0.0
        self._complete_audio = False
        self._last_received_loud = 0.0
        self._finish_task = None

    # ---- управление из других потоков

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, samples):
        if self.track and self.listen:
            self.track.feed(samples)

    def stop(self):
        self._cancel = True
        self.hush()
        if self.loop and self._stop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass        # сессия уже закрылась сама

    # ---- озвучка голосом OpenAI (вызывается из других потоков)

    def speak(self, text):
        """Попросить модель прочитать текст вслух. False — сессия ещё не готова."""
        if not self.ws or not self.loop or self._cancel or not text.strip():
            return False
        self.hush()
        with self._playback_lock:
            self._complete_audio = self.playback_mode() == "complete"
            self._audio_on = True
            self._speaking = True
        self.log.debug("OpenAI playback mode: %s", "complete" if self._complete_audio else "stream")
        self._said_speaking, self._frames_heard = False, 0   # для подробного журнала: дошёл ли звук
        self.log.debug("Live speech: %d characters", len(text))
        self._deadline = time.time() + min(120, max(20, len(text) * 0.08))
        asyncio.run_coroutine_threadsafe(self._append(text), self.loop)
        return True

    def hush(self):
        """Замолчать: звук модели больше не пускаем, недоигранное выбрасываем."""
        with self._playback_lock:
            self._playback_serial += 1
            self._audio_on = False
            self._speaking = False
            self._buffered_audio.clear()
            self._buffered_seconds = 0.0
            self.audio.clear()

    def speaking(self):
        """Модель читает или звук ещё доигрывает (для глушения микрофона)."""
        if self._speaking and time.time() > self._deadline:
            self.hush()
        return self._speaking or (self._complete_audio and bool(self.audio._queue.unfinished_tasks)) or time.time() - self.audio.last_loud < 0.6

    async def _append(self, text):
        """Текст для чтения — кусками до SPEAK_CHUNK байт (как в jauvex)."""
        chunk = ""
        pieces = []
        for ch in text:
            if len((chunk + ch).encode()) > SPEAK_CHUNK:
                pieces.append(chunk)
                chunk = ""
            chunk += ch
        if chunk:
            pieces.append(chunk)
        try:
            for piece in pieces:
                await self.ws.send(json.dumps({
                    "type": "session.context.append",
                    "channel": "speakable",
                    "content": [{"type": "input_text", "text": piece}],
                }))
        except Exception as e:
            self.log.warning("live speak: %s", e)
            self._speaking = False

    # ---- внутренности

    def _text(self, text):
        """Фраза распознана (из цикла asyncio) — в очередь печати, цикл не ждёт."""
        self._texts.put(text)

    def _text_loop(self):
        while True:
            text = self._texts.get()
            if text is None:
                return
            try:
                self.on_text(text)
            except Exception:
                self.log.exception("Live speech: phrase was not typed")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            self.log.exception(t("log.live_crash"))
            self.on_state("error", str(e))
        finally:
            self.track = None
            self.ws = None
            self.audio.close()
            self.loop.close()
            self._texts.put(None)        # поток печати допечатает очередь и закончится
            self.on_state("closed", "")

    def _session(self):
        """Параметры сессии OpenAI."""
        return {
            "model": MODEL,
            "instructions": instructions(self.lang, self.prompt),
            "audio": {"output": {"voice": "cove"}},
            "delegation": {"type": "client", "ack_filler": False},
        }

    def _post_call(self, sdp, headers):
        """Отдаём SDP-предложение, получаем SDP-ответ и id звонка."""
        body = json.dumps({"sdp": sdp, "session": self._session()}).encode()
        req = urllib.request.Request(CALL_URL, data=body, method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            answer = resp.read().decode()
            location = resp.headers.get("location") or ""
            call_id = next((p for p in location.split("/") if re.match(r"^rtc_[\w-]+$", p)), None)
            call_id = call_id or resp.headers.get("openai-session-id")
        if not call_id or not answer.strip():
            raise RuntimeError(t("err.live_no_call"))
        return answer, call_id

    async def _main(self):
        self._stop = asyncio.Event()
        if self._cancel:
            return
        self.on_state("connecting", "")
        headers = make_headers(*credentials())

        # без STUN: сбор адресов мгновенный (со STUN ждёт 5 с), сервер OpenAI доступен напрямую
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.track = MicTrack(self.loop, self)
        pc.addTrack(self.track)
        pc.createDataChannel("oai-events")

        # голос модели: в динамики — только когда сами попросили прочитать ответ
        players = []

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                players.append(asyncio.ensure_future(self._play(track)))

        try:
            await pc.setLocalDescription(await pc.createOffer())
            answer, call_id = await asyncio.to_thread(self._post_call, pc.localDescription.sdp, headers)
            if self._cancel:
                return
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            self.log.info(t("log.live_call"), call_id)

            async with websockets.connect(SIDEBAND_URL + call_id, additional_headers=headers) as ws:
                self.ws = ws
                self.on_state("live", "")
                reader = asyncio.create_task(self._read(ws))
                stopper = asyncio.create_task(self._stop.wait())
                if self._cancel:
                    self._stop.set()
                await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                try:
                    await ws.send(json.dumps({"type": "session.close"}))
                except Exception:
                    pass
                reader.cancel()
        finally:
            self.ws = None
            for task in players:
                task.cancel()
            await pc.close()

    async def _play(self, track):
        """Кадры звука от OpenAI -> AudioOut (или в никуда, если читать не просили)."""
        while True:
            try:
                frame = await track.recv()
            except (MediaStreamError, asyncio.CancelledError):
                return
            if not self._audio_on:
                continue
            self._frames_heard = getattr(self, "_frames_heard", 0) + 1
            channels = len(frame.layout.channels)
            pcm = frame.to_ndarray()
            pcm = pcm.T if frame.format.is_planar else pcm.reshape(-1, channels)
            pcm = np.ascontiguousarray(pcm, dtype=np.int16)
            with self._playback_lock:
                if not self._audio_on:
                    continue
                if self._complete_audio:
                    if int(np.abs(pcm).max(initial=0)) > LOUD:
                        self._last_received_loud = time.monotonic()
                    self._buffered_audio.append((pcm.copy(), frame.sample_rate, channels))
                    self._buffered_seconds += len(pcm) / frame.sample_rate
                    if self._buffered_seconds > 180:
                        self.log.warning("OpenAI: speech exceeded the 180 s buffer and was cancelled")
                        self.hush()
                else:
                    self.audio.put(pcm, frame.sample_rate, channels)

    async def _finish_buffered_audio(self, serial):
        # Media and turn.done travel on separate transports. Allow the RTP tail
        # to arrive, then wait for a short quiet tail, with a bounded deadline.
        started = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            with self._playback_lock:
                if serial != self._playback_serial or not self._audio_on:
                    return
                now = time.monotonic()
                if now - started < 0.8:
                    continue
                if now - self._last_received_loud < 0.6 and now - started < 5:
                    continue
                if now - self._last_received_loud < 0.6:
                    self.log.warning("OpenAI: audio stream did not finish after turn.done. Playback cancelled")
                    self.hush()
                    return
                frames, self._buffered_audio = self._buffered_audio, []
                seconds, self._buffered_seconds = self._buffered_seconds, 0.0
                self._audio_on = False
                for pcm, rate, channels in frames:
                    self.audio.put(pcm, rate, channels)
                self._speaking = False
                self.log.info("OpenAI: loaded %.2f s of audio; playback started", seconds)
                return

    async def _flusher(self, buf):
        """Закрывает фразу по паузе в кусочках текста."""
        while True:
            await asyncio.sleep(0.2)
            if buf.due():
                text = buf.flush()
                if text:
                    self._text(text)

    async def _read(self, ws):
        buf = TranscriptBuffer()
        flusher = asyncio.create_task(self._flusher(buf))
        try:
            await self._read_loop(ws, buf)
        finally:
            flusher.cancel()

    async def _read_loop(self, ws, buf):
        async for raw in ws:
            result = parse_event(raw)
            if not result:
                continue
            kind, value = result

            if kind == "speaking":
                if not self._said_speaking:
                    self._said_speaking = True
                    self.log.debug("Live model speaking (audio enabled: %s)", self._audio_on)
                continue
            if kind == "spoken":
                if self._complete_audio and self._audio_on:
                    if self._finish_task:
                        self._finish_task.cancel()
                    self._finish_task = asyncio.create_task(self._finish_buffered_audio(self._playback_serial))
                else:
                    self._speaking = False      # хвост доиграет, speaking учитывает очередь
                self.log.debug("Live model finished speaking (audio frames: %d)", self._frames_heard)
                continue
            if kind == "fragment":
                if not self._speaking:
                    self._audio_on = False  # говорит пользователь — ответы модели вслух не нужны
                buf.add(value)
            elif kind == "delegation":
                text = buf.on_delegation()
                if text:
                    self._text(text)
                # говорим модели, что текст ушёл агенту (как jauvex)
                await ws.send(json.dumps({
                    "type": "delegation.context.append",
                    "delegation_item_id": value,
                    "channel": "commentary",
                    "content": [{"type": "input_text", "text": "The transcript is sent directly to the coding agent."}],
                }))
            elif kind == "transcript":
                text = buf.on_final(value)
                if text:
                    self._text(text)
            elif kind == "error":
                self.log.warning("live: %s", value)
                self.on_state("error", value)
