"""Вкладка «Тесты»: база текстов и записей для проверки распознавания.

Тексты — tests/corpus/texts.json; звук — tests/corpus/audio/<источник>/<id>.wav
(источник: me1…me3 — три ваших слота, у записи рядом <id>.json с названием микрофона;
silero, system, grok, openai — синтез); реальные шумы — tests/corpus/audio/noise/*.wav.
"""

import json
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

import audio_out

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus"
AUDIO = CORPUS / "audio"

STT = ("whisper", "gigaam", "zipa", "apple", "grok", "openai", "live")   # распознаватели
SLOTS = ("me1", "me2", "me3")                                           # ваши записи: три слота на текст
VOICES = ("silero", "system", "grok", "openai")                          # голоса озвучки (синтез)
KINDS = ("phrase", "phrase+command", "command", "paragraph", "paragraph+command")


def texts():
    """Тексты базы: [{id, kind, text, expect, commands}]."""
    try:
        return json.loads((CORPUS / "texts.json").read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError):
        return []


def mic_name(name):
    """Название микрофона записи: выбранный вручную или микрофон macOS сейчас."""
    if name:
        return name
    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:
        return ""


def sources():
    """Все источники записей: три ваших слота, дальше голоса озвучки."""
    return list(SLOTS) + list(VOICES)


def recorded():
    """Какие тексты чем записаны: {источник: [id]}; микрофоны ваших записей {"me1/p1": название};
    и сколько записано шумов."""
    out = {v: sorted(p.stem for p in (AUDIO / v).glob("*.wav")) for v in sources()}
    mics = {}
    for slot in SLOTS:
        for meta in (AUDIO / slot).glob("*.json"):
            try:
                mics[f"{slot}/{meta.stem}"] = json.loads(meta.read_text(encoding="utf-8")).get("mic", "")
            except (OSError, ValueError):
                pass
    out["mics"] = mics
    out["noise"] = len(list((AUDIO / "noise").glob("*.wav")))
    return out


def audio_path(source, item_id):
    """Файл записи (None — такого источника нет: защита от чужих путей)."""
    if source not in sources() or "/" in item_id or ".." in item_id:
        return None
    return AUDIO / source / f"{item_id}.wav"


def info(recorder=None, playing=None, synth=None, running=False):
    """Для окна: тексты, записи, что пишется, что играет и как идёт синтез."""
    rec = ({"id": recorder.id, "slot": recorder.slot, "seconds": round(time.time() - recorder.started, 1)}
           if recorder and recorder.id else None)
    return {"items": texts(), "sources": sources(), "recorded": recorded(), "recording": rec, "playing": playing,
            "synth": synth.state if synth else None, "synth_errors": getattr(synth, "last_errors", []),
            "run": run_state(running)}


def run_state(running=False):
    """Прогон: {done, total, now, finished, error, report} из progress.json; running — процесс жив."""
    try:
        st = json.loads((CORPUS / "results" / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st["running"] = bool(running)
    report = CORPUS / "results" / "report.html"
    st["has_report"] = report.is_file()
    st["report_at"] = time.strftime("%d.%m %H:%M", time.localtime(report.stat().st_mtime)) if st["has_report"] else ""
    return st


RATE = 16000


class Recorder:
    """Запись вашего голоса в слот: start(id, slot) … stop() -> audio/<slot>/<id>.wav (16 кГц, моно) + <id>.json.

    Пока идёт запись, детектор речи Voice на паузе — сказанное не печатается в терминал."""

    def __init__(self, log):
        self.log = log
        self.id = None              # какой текст пишем сейчас
        self.slot = None            # в какой слот (me1…me3)
        self.mic = ""               # каким микрофоном
        self.started = 0.0
        self._stream = None
        self._chunks = []
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        self._chunks.append(indata[:, 0].copy())

    def start(self, item_id, slot, chosen_mic=None):
        self.stop()
        self._chunks = []
        device = audio_out.index(chosen_mic, "in")
        self.mic = mic_name(chosen_mic if device is not None else None)
        self.slot = slot
        with audio_out.pa_lock():
            self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                          device=device, callback=self._callback)
            self._stream.start()
        self.id, self.started = item_id, time.time()
        self.log.info("Test recording started for “%s” in slot %s (%s)", item_id, slot, self.mic)

    def stop(self):
        """Закончить запись и сохранить файл; вернуть длину в секундах (0 — ничего не писали)."""
        with self._lock:
            if self._stream is None:
                return 0.0
            with audio_out.pa_lock():
                try:
                    self._stream.stop()
                finally:
                    self._stream.close()
            self._stream = None
            item_id, self.id = self.id, None
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
            if len(audio) < RATE // 2:         # меньше 0,5 с — случайное нажатие, не сохраняем
                self.log.info("Test recording “%s” was too short and was skipped", item_id)
                return 0.0
            path = AUDIO / self.slot / f"{item_id}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(path, audio, RATE, subtype="PCM_16")
            path.with_suffix(".json").write_text(json.dumps(
                {"mic": self.mic, "at": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False), encoding="utf-8")
            self.log.info("Test recording “%s” saved in slot %s (%s, %.1f s)", item_id, self.slot, self.mic,
                          len(audio) / RATE)
            return len(audio) / RATE


# ---------- синтез: те же тексты голосами озвучки (Silero, macOS, Grok, OpenAI)

PAD_S = 0.5     # тишина до и после речи — как в живой записи (детектору речи нужна пауза)


def _to_wav16(src, dst):
    """Любой звук (aiff / mp3 / wav) -> WAV 16 кГц моно, с тишиной PAD_S по краям."""
    import subprocess
    tmp = dst.with_suffix(".tmp.wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(tmp)],
                   check=True, capture_output=True)
    audio, rate = sf.read(tmp, dtype="float32")
    tmp.unlink()
    pad = np.zeros(int(PAD_S * RATE), dtype=np.float32)
    sf.write(dst, np.concatenate([pad, audio, pad]), RATE, subtype="PCM_16")


class Synth:
    """Озвучить тексты базы голосами озвучки: run(voices, redo) в своём потоке; state — для окна."""

    def __init__(self, app, log):
        self.app = app
        self.log = log
        self.state = None           # {"voice", "done", "total", "errors"} — пока идёт синтез

    def run(self, voices, redo=False):
        if self.state:
            return {"ok": False, "error": "Speech generation is already running"}
        jobs = [(v, it) for v in voices if v in VOICES for it in texts()
                if redo or not (AUDIO / v / f"{it['id']}.wav").is_file()]
        if not jobs:
            return {"ok": True, "total": 0}
        self.state = {"voice": "", "done": 0, "total": len(jobs), "errors": []}
        threading.Thread(target=self._run, args=(jobs,), daemon=True).start()
        return {"ok": True, "total": len(jobs)}

    def _run(self, jobs):
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="voice-tests-"))
        try:
            for voice, item in jobs:
                self.state["voice"] = voice
                dst = AUDIO / voice / f"{item['id']}.wav"
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    src = self._render(voice, item["text"], tmpdir)
                    _to_wav16(src, dst)
                    self.log.info("Test speech %s for “%s” is ready", voice, item["id"])
                except Exception as e:
                    self.state["errors"].append(f"{voice} {item['id']}: {e}")
                    self.log.warning("Test speech %s for “%s” failed: %s", voice, item["id"], e)
                self.state["done"] += 1
        finally:
            self.log.info("Test speech complete (%d, errors %d)", self.state["total"], len(self.state["errors"]))
            self.last_errors = self.state["errors"]
            self.state = None

    def _render(self, voice, text, tmpdir):
        """Синтез одного текста -> путь к временному файлу звука."""
        import subprocess
        app = self.app
        if voice == "system":
            out = tmpdir / "say.aiff"
            subprocess.run(["say", "-v", app.speaker.voice, "-r", str(app.speaker.rate), "-o", str(out), "--", text],
                           check=True, capture_output=True)
            return out
        if voice == "silero":
            from local_voice import SILERO_RATE
            gap = np.zeros(int(0.25 * SILERO_RATE), dtype=np.float32)
            parts = []
            for audio, _ in app.silero.render(text, rate=app.speaker.rate):
                parts += [audio.astype(np.float32), gap]
            out = tmpdir / "silero.wav"
            sf.write(out, np.concatenate(parts), SILERO_RATE)
            return out
        tts = app.grok_tts if voice == "grok" else app.oa_tts
        mp3 = tts._synth(text, tts.voice(), "ru", 1.0)
        out = tmpdir / f"{voice}.mp3"
        out.write_bytes(mp3)
        return out
