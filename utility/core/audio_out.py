"""Звуковые устройства Voice: «Автоматически» (как выбрано в macOS) или «Вручную» (своё, независимо от системы).

Настройки: audio_in / audio_out = {"mode": "auto" | "manual", "name": "название устройства"}.
Вывод: в «Автоматически» — afplay (как раньше); в «Вручную» — отдельный процесс audio_play.py на выбранное
устройство (CoreAudio иногда виснет — пусть виснет он, а не Voice: сторож его убьёт).
play() возвращает subprocess.Popen: poll(), wait(), terminate().
"""

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import sounddevice as sd
import soundfile as sf

import audio_dev

CFG = {}
LOG = logging.getLogger("orca-voice")

PLAYER = Path(__file__).resolve().parent / "audio_play.py"

# PortAudio в процессе Voice (микрофон, запись тестов, голос OpenAI Live): открытие и закрытие потоков — по очереди
PA_LOCK = threading.RLock()     # повторно в том же потоке можно: index() внутри открытия потока
LOCK_WAIT_S = 3         # ждать очереди не дольше (очередь сама зависла — идём без неё)
HANG_EXTRA_S = 5        # звук не кончился за «длина + запуск + столько» — процесс завис, убиваем
START_S = 3             # запас на запуск процесса проигрывателя


class pa_lock:
    """Очередь к PortAudio: with pa_lock(): … (ждёт не дольше LOCK_WAIT_S)."""

    def __enter__(self):
        self._got = PA_LOCK.acquire(timeout=LOCK_WAIT_S)
        if not self._got:
            LOG.warning("audio: device queue busy for over %d s — continuing without it", LOCK_WAIT_S)
        return self

    def __exit__(self, *exc):
        if self._got:
            PA_LOCK.release()


class Locked:
    """Streaming PortAudio в with: открыть + запустить и остановить + закрыть — в очереди pa_lock."""

    def __init__(self, factory):
        self._factory = factory

    def __enter__(self):
        with pa_lock():
            self.stream = self._factory()
            try:
                self.stream.start()
            except Exception:
                try:
                    self.stream.close()          # could not start (устройство занято, нет доступа) — не оставлять открытым
                except Exception:
                    pass
                raise
        return self.stream

    def __exit__(self, *exc):
        with pa_lock():
            try:
                self.stream.stop()
            except Exception:
                pass                             # поток уже мёртв (PortAudio перезапущен, устройство пропало)
            try:
                self.stream.close()
            except Exception:
                pass


def configure(cfg):
    """Настройки Voice (вызывает app при запуске и после сохранения)."""
    global CFG
    CFG = cfg


def chosen(kind):
    """Название выбранного вручную устройства (kind: in / out) или None — «как в macOS»."""
    o = CFG.get("audio_" + kind) or {}
    return (o.get("name") or None) if o.get("mode") == "manual" else None


def output_key():
    """Режим и выбранный выход; помогает потоковому голосу заметить смену настройки."""
    return ((CFG.get("audio_out") or {}).get("mode") == "manual", chosen("out"))


def output_available():
    """Ручной выход должен быть подключён; system output вместо него не подставляем."""
    manual, name = output_key()
    return not manual or bool(name and audio_dev.output_info(name)[0] == name)


def fallback_in():
    """Выбранный вручную микрофон не подключён: True — пока слушать микрофон macOS по умолчанию
    (audio_in.fallback = "default", так по умолчанию), False — не слушать вовсе и ждать его ("wait")."""
    return (CFG.get("audio_in") or {}).get("fallback", "default") != "wait"


def devices():
    """Списки устройств для окна: {"in": [названия], "out": [названия]} (PortAudio: что видно сейчас)."""
    # PortAudio здесь не перезапускаем — оборвался бы открытый микрофон; новые устройства он видит
    # после смены устройства в macOS (Listener сам перечитывает список)
    ins, outs = [], []
    with pa_lock():                    # не посреди перезапуска PortAudio («PortAudio not initialized»)
        found = sd.query_devices()
    for d in found:
        if d["max_input_channels"] > 0 and d["name"] not in ins:
            ins.append(d["name"])
        if d["max_output_channels"] > 0 and d["name"] not in outs:
            outs.append(d["name"])
    return {"in": ins, "out": outs}


def index(name, kind):
    """Номер устройства PortAudio по названию (kind: in / out); None — нет такого (отключили)."""
    if not name:
        return None
    key = "max_input_channels" if kind == "in" else "max_output_channels"
    with pa_lock():
        found = sd.query_devices()
    for i, d in enumerate(found):
        if d["name"] == name and d[key] > 0:
            return i
    return None


def duration(path):
    """Длина звука в секундах (по заголовку файла); не прочитать — 60 с."""
    try:
        return sf.info(str(path)).duration
    except Exception:
        return 60.0


def _guard(proc, path, limit):
    """Собирает stderr, журналирует ошибки и останавливает зависший проигрыватель."""
    device = chosen("out") if output_key()[0] else "system output"
    started = time.monotonic()
    def check():
        try:
            _, error = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            LOG.warning("audio: playback on “%s” hung (%s) and was stopped", device, Path(path).name)
            proc.kill()
            proc.communicate()
            return
        LOG.debug("audio: finished pid=%s file=%s output=%s code=%s time=%.3f s",
                  proc.pid, Path(path).name, device, proc.returncode, time.monotonic() - started)
        if proc.returncode > 0:
            reason = (error or b"").decode("utf-8", errors="replace").strip()
            LOG.warning("audio: playback error on “%s” (%s), code %s: %s",
                        device, Path(path).name, proc.returncode, reason[-4000:] or "no reason given")
        # terminate/kill при «стоп» — штатная отмена, не ошибка воспроизведения.
    watcher = threading.Thread(target=check, name="audio-playback-log", daemon=True)
    watcher.start()
    return watcher


def play(path, volume=1.0):
    """Сыграть файл (WAV / MP3 / M4A): выбранное вручную устройство (отдельный процесс) или как в macOS (afplay)."""
    name = chosen("out")
    manual = output_key()[0]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
    args = ([sys.executable, "-I", str(PLAYER), str(path), name or "", str(volume)] if manual
            else ["afplay", "-v", str(volume), str(path)])
    try:
        proc = subprocess.Popen(args, **quiet)
    except OSError as exc:
        LOG.warning("audio: could not start playback on “%s” (%s): %s",
                    name if manual else "system output", Path(path).name, exc)
        raise
    _guard(proc, path, duration(path) + START_S + HANG_EXTRA_S)
    return proc


def say_args():
    """Для голоса macOS (say): ["-a", устройство] при ручном выборе."""
    name = chosen("out")
    return ["-a", name] if name else []


# ---------- наушники или динамики (эхо «авто» в блоке «Звук»)

# Bluetooth — обычно наушники, но бывают колонки: по этим словам в названии считаем колонкой (эхо есть)
SPEAKER_WORDS = ("speaker", "колонк", "динамик", "jbl go", "jbl charge", "jbl flip", "soundlink", "homepod",
                 "sonos", "boom", "soundcore motion", "marshall stanmore", "marshall acton")
HEADPHONE_WORDS = ("headphone", "headset", "наушник", "гарнитур", "airpods", "buds", "earpods", "beats")
KIND_CACHE_S = 2.0      # тип выхода кэшируется (окно спрашивает каждые 0,7 с, детектор — на каждую фразу)
_kind_cache = (0.0, None, None)


def classify(name, transport, source):
    """(название, тип подключения, источник встроенного выхода) → "headphones" | "speakers".
    Сомневаемся — «динамики»: лишняя защита от эха безопаснее, чем Voice, перебивший сам себя (случай 20:50)."""
    low = (name or "").lower()
    if transport == "builtin":
        if source == "headphones" or any(w in low for w in HEADPHONE_WORDS):
            return "headphones"           # гнездо 3,5 мм: «Внешние наушники» / источник «наушники»
        return "speakers"
    if transport == "bluetooth":
        return "speakers" if any(w in low for w in SPEAKER_WORDS) else "headphones"
    if transport == "usb":
        return "headphones" if any(w in low for w in HEADPHONE_WORDS) else "speakers"
    return "speakers"                     # HDMI, DisplayPort, AirPlay, виртуальные, неизвестные — слышно в комнате


def out_kind():
    """Куда сейчас идёт звук Мака: "headphones" | "speakers" (выход «Вручную» или как в macOS)."""
    global _kind_cache
    name = chosen("out")
    at, key, kind = _kind_cache
    if key == name and kind and time.time() - at < KIND_CACHE_S:
        return kind
    info = audio_dev.output_info(name)
    kind = classify(*info)
    _kind_cache = (time.time(), name, kind)
    return kind


def out_name():
    """Название выхода звука Мака: выбранный вручную или как в macOS."""
    return chosen("out") or audio_dev.names()[1]


def in_name():
    """Название микрофона Мака: выбранный вручную или как в macOS."""
    return chosen("in") or audio_dev.names()[0]
