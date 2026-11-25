"""Локальные модели без интернета: GigaAM (распознавание русского) и Silero (русский голос).

Дополнительные варианты к Whisper и голосу macOS. Модели — в lib/models/gigaam и lib/models/silero,
загружаются при первом обращении (torch тяжёлый — не грузим его, пока вариант не выбран).
"""

import os
import re
import subprocess

import audio_out
import tempfile
import threading
import time

import numpy as np

from libdir import lib_dir

GIGAAM_MODEL = "v3_e2e_rnnt"      # с пунктуацией и нормализацией текста
GIGAAM_MAX_S = 24                 # GigaAM.transcribe берёт не больше 25 с — длинное режем
SILERO_FILE = "v5_5_ru.pt"
SILERO_VOICES = ("xenia", "kseniya", "baya", "aidar", "eugene")
SILERO_RATE = 48000
CYRILLIC = re.compile(r"[а-яё]", re.I)
NOISE = re.compile(r"^[\s.,!?¡¿-]*$")


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


# ---------- распознавание: GigaAM

class GigaAM:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self.model is not None:
                return
            import gigaam
            t = time.time()
            root = str(lib_dir(self.cfg) / "models" / "gigaam")
            self.model = gigaam.load_model(GIGAAM_MODEL, download_root=root, device=_device())
            self._run(np.zeros(16000, dtype=np.float32))     # прогрев: первый проход на MPS долгий
            self.log.info("GigaAM ready (%.1f s)", time.time() - t)

    def transcribe(self, wav_bytes):
        """WAV (16 кГц, моно, 16 бит) -> текст. Пустая строка, если это не речь."""
        self.load()
        pcm = np.frombuffer(wav_bytes[44:], dtype=np.int16).astype(np.float32) / 32767
        step = GIGAAM_MAX_S * 16000
        parts = [self._run(pcm[i:i + step]) for i in range(0, len(pcm), step)]
        text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
        return "" if NOISE.match(text) else text

    def _run(self, samples):
        """Кусок до 24 с: GigaAM читает только файл — пишем временный WAV."""
        import soundfile as sf
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            sf.write(path, samples, 16000, subtype="PCM_16")
            result = self.model.transcribe(path)
            return getattr(result, "text", result) or ""
        finally:
            os.unlink(path)


# ---------- озвучка: Silero

class Silero:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.model = None
        self._lock = threading.Lock()
        self._token = 0            # номер текущей фразы: новая фраза или stop() обрывают старую
        self._playing = False
        self._proc = None          # afplay: играет на текущем выходе macOS, как say (PortAudio помнит выход с запуска)

    def load(self):
        with self._lock:
            if self.model is not None:
                return
            import torch
            t = time.time()
            path = lib_dir(self.cfg) / "models" / "silero" / SILERO_FILE
            imp = torch.package.PackageImporter(str(path))
            model = imp.load_pickle("tts_models", "model")
            model.to("cpu")       # на CPU и так в 100 раз быстрее речи
            self.model = model  # Ready only after initialization has completed.
            self.log.info("Silero ready (%.1f s)", time.time() - t)

    @staticmethod
    def suits(text):
        """Silero — только русский: текст без кириллицы читает системный голос."""
        return bool(CYRILLIC.search(text or ""))

    def voice(self, name=None):
        name = name or self.cfg.get("silero_voice", "xenia")
        return name if name in SILERO_VOICES else SILERO_VOICES[0]

    def speak(self, text, voice=None, rate=190):
        """Озвучить в фоне; возвращает сразу."""
        self._token += 1
        token = self._token
        self._kill()
        self._playing = True
        threading.Thread(target=self._speak, args=(text, self.voice(voice), rate, token), daemon=True).start()

    def _speak(self, text, voice, rate, token):
        try:
            self.load()
            speed = max(0.6, min(1.6, rate / 190))       # 190 слов/мин в Orca = обычный темп Silero
            for chunk in self._chunks(text):
                if token != self._token:
                    return
                audio = self.model.apply_tts(text=chunk, speaker=voice, sample_rate=SILERO_RATE).numpy()
                if speed != 1.0:
                    audio = self._tempo(audio, speed)
                if token != self._token:
                    return
                self._play(audio, token)
        except Exception as e:
            self.log.warning("silero: %s", e)
        finally:
            if token == self._token:
                self._playing = False

    def render(self, text, voice=None, rate=190):
        """Звук по кускам (по предложениям): (float32, частота) — для отправки на телефон."""
        self.load()
        speed = max(0.6, min(1.6, rate / 190))
        for chunk in self._chunks(text):
            audio = self.model.apply_tts(text=chunk, speaker=self.voice(voice), sample_rate=SILERO_RATE).numpy()
            if speed != 1.0:
                audio = self._tempo(audio, speed)
            yield audio, SILERO_RATE

    @staticmethod
    def _chunks(text, limit=800):
        """Silero не любит длинные строки — режем по предложениям."""
        out, cur = [], ""
        for sentence in re.split(r"(?<=[.!?…])\s+", text):
            if cur and len(cur) + len(sentence) > limit:
                out.append(cur)
                cur = ""
            cur = (cur + " " + sentence).strip()
        if cur:
            out.append(cur)
        # предложение длиннее limit (абзац без точек, длинный список) — режем по запятой / пробелу, а не обрубаем:
        # хвост раньше молча не озвучивался
        pieces = []
        for c in out:
            while len(c) > limit:
                cut = max(c.rfind(", ", 0, limit), c.rfind("; ", 0, limit))
                if cut < limit // 2:
                    cut = c.rfind(" ", 0, limit)
                if cut < limit // 2:
                    cut = limit - 1                  # одно сплошное «слово» — делать нечего, режем как есть
                pieces.append(c[:cut + 1].strip())
                c = c[cut + 1:].strip()
            if c:
                pieces.append(c)
        return pieces

    @staticmethod
    def _tempo(audio, speed):
        """Темп без сложной обработки: простая передискретизация (тон чуть меняется)."""
        n = int(len(audio) / speed)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)

    def _play(self, audio, token):
        """Кусок звука -> временный WAV -> afplay; ждём конца или обрыва."""
        import soundfile as sf
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            sf.write(path, audio, SILERO_RATE, subtype="PCM_16")
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
