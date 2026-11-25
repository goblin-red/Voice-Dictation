"""Микрофон + простой детектор речи по громкости: режет поток на короткие фразы."""

import io
import os
import queue
import threading
import time
import wave
from collections import deque

import numpy as np
import sounddevice as sd

import audio_dev
from audio_diagnostics import AudioDiagnostics
import audio_out
from i18n import t

RATE = 16000
FRAME = 480            # 30 мс
FRAME_MS = 30
WEAK_MS = 30          # vad.min_command_ms по умолчанию: короче min_speech_ms, но громких хотя бы столько — короткое слово:
WEAK_MAX_MS = 1500    # …и вся фраза не длиннее — распознаём, но выполняем только команду (app.py, weak)
DEVICE_CHECK_S = 10     # как часто сверять устройство по умолчанию в macOS
MISSING_CHECK_S = 5     # выбранный микрофон не подключён: как часто проверять, не подключили ли
REINIT_MIN_S = 60       # список устройств CoreAudio узнать не удалось — перечитывать PortAudio не чаще
FRAMES_MAX = 4000       # очередь кадров (2 мин звука, ~8 МБ): поток разбора отстал или перезапускается — старое выбрасываем
RESTART_S = (1, 2, 5, 10, 30)   # поток микрофона упал — пауза перед новым запуском (растёт, если падает подряд)
PREROLL_FRAMES = 10    # 300 мс звука до начала речи, чтобы не срезать первый слог
AFTER_VOICE_FRAMES = 25   # 750 мс: голос на слух уже кончился (тишина в конце), а «звучит» ещё — первое слово не теряем
OUTPUT_CHECK_S = 0.1   # как часто спрашивать macOS, занятые ли динамики
DEAF_MAX_S = 20        # «слушать после сигнала»: запасной предел, если сигнал так и не прозвучал
CUT_LOOK_FRAMES = 66   # 2 с: длинную речь режем в самом тихом месте последних 2 секунд, а не посреди слова
CUT_SMOOTH = 3         # тишину ищем окном в 90 мс (одиночный тихий кадр внутри слова — не пауза)
# vad.auto_level — «Автоподстройка громкости»: минимальная громкость речи — доля средней громкости последних фраз
AUTO_PHRASES = 10      # сколько последних фраз помним (поднёс микрофон ближе / дальше — подстроится за 3–4 фразы)
AUTO_START = 3         # пока фраз меньше — работает ползунок «Минимальная громкость речи»
AUTO_SHARE = 0.4       # порог — такая доля громкости речи
AUTO_FLOOR = 0.006     # ниже не опускаем (шорохи)
AUTO_CEIL = 0.06       # выше не поднимаем (тихие слова не терять) — как правый край ползунка


def quiet_cut(frames):
    """Где резать длинную речь: самое тихое место (окно CUT_SMOOTH кадров) в последних CUT_LOOK_FRAMES кадрах."""
    start = max(1, len(frames) - CUT_LOOK_FRAMES)
    rms = [float(np.sqrt(np.mean(f ** 2))) for f in frames[start:]]
    if len(rms) < CUT_SMOOTH:
        return len(frames)
    smooth = [sum(rms[i:i + CUT_SMOOTH]) for i in range(len(rms) - CUT_SMOOTH + 1)]
    best = min(range(len(smooth)), key=smooth.__getitem__)
    return start + best + CUT_SMOOTH // 2 + 1        # середина тихого окна


class PhraseAudio(bytes):
    def __new__(cls, data, pause_before_s, interrupted_voice=False):
        value = super().__new__(cls, data)
        value.pause_before_s = pause_before_s
        value.interrupted_voice = bool(interrupted_voice)
        return value


def to_wav(samples, pause_before_s=None, interrupted_voice=False):
    """float32 [-1..1] -> байты WAV 16 бит моно."""
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    data = buf.getvalue()
    return data if pause_before_s is None and not interrupted_voice else PhraseAudio(data, pause_before_s, interrupted_voice)


class Listener(threading.Thread):
    """Слушает микрофон и вызывает on_phrase(wav_bytes, long, spoken, final=True) на каждую фразу.

    long=True   — кусок длинной речи (например, пока читает голос): в нём ищем только «заткнись».
    spoken=True — во время фразы звучал наш голос: её нельзя печатать, только искать «заткнись».
    dictation   — включён голосовой ввод: фразы до dictation_max_s, длинная речь режется без повторов.
    barge       — «любая диктовка» поверх голоса: куски речи поверх голоса тоже идут на ввод (без повторов).
    final=False — кусок ещё не законченной речи (длинная фраза режется), а не её конец.
    tap — если задан, звук уходит туда (режим OpenAI), а свой детектор речи молчит.
    hold — если возвращает True (говорит наш голос), в tap идёт тишина, а звук слушаем сами:
           так ответ ИИ не попадёт в OpenAI, а «заткнись» всё равно услышим.
    """

    def __init__(self, vad_cfg, on_phrase, log):
        super().__init__(daemon=True)
        self.cfg = vad_cfg
        self.on_phrase = on_phrase
        self.log = log
        self.capture_open = False
        self.paused = False
        self.level = 0.0          # текущая громкость (для меню)
        self.threshold = 0.0      # текущий порог начала речи (для шкалы)
        self.auto_level = 0.0     # «Автоподстройка громкости»: найденная минимальная громкость речи (0 — ещё нет)
        self._auto = deque(maxlen=AUTO_PHRASES)   # громкость последних фраз
        self.in_speech = False    # сейчас идёт фраза (детектор речи слышит голос)
        self.last_voice = 0.0     # когда последний раз звучал голос человека (time.time)
        self.tap = None           # куда отдавать звук в режиме OpenAI
        self.on_start = None      # функция (id): фраза пошла в потоковый распознаватель (порядок печати)
        self.on_drop = None       # функция (id): фразу потокового распознавателя забыли — итога по ней не будет
        # функция (wav): начало фразы, пока ещё говорят — слово-ключ на лету; False — больше не проверять эту фразу
        self.on_probe = None
        self.probe_ms = (600, 300, 2400)   # первая проверка, шаг, последняя — мс от начала фразы
        self._probe_at = 0                 # следующая проверка (мс); 0 — не проверяем
        self.stream = None        # диктовка «Apple»: потоковый распознаватель (start / feed / end / discard)
        self._sid = None          # id фразы в потоковом распознавателе
        self.hold = None          # функция: True — пока говорит наш голос
        self.audio_diagnostics = AudioDiagnostics(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "audio"), log)
        self.diagnostic_input_name = "system-default"
        self.on_input_device = None
        self.hush = False         # не слушать никого — ни Мак, ни планшет (запись во вкладке «Тесты»)
        self.dictation = False    # голосовой ввод текста в штатном режиме
        self.barge = False        # «любая диктовка»: речь поверх голоса печатается, а голос замолкает
        self.echo_free = None     # функция: True — голос в наушниках, эха в микрофоне нет
        self.short_sound_stop = None  # callback: enabled only with explicit echo off
        self.on_barge = None      # функция: заговорили поверх голоса (barge + echo_free) — голос замолкает сразу
        self._barge_active = False      # эта фраза действительно прервала звучащий голос, не его хвост
        self._interrupted_sids = set()  # такие потоковые фразы печатаем только после проверки итогового текста
        self.mute_on_speech = True  # пока говорит голос — микрофон полностью выключен
        self.mute_on_play = False   # пока любой звук идёт в динамики — микрофон полностью выключен
        self.self_playing = None    # функция: True — звучит наш голос (наш процесс в опросе macOS не считаем)
        self.input_name = None      # микрофон, выбранный вручную (None — macOS default)
        self.missing = None         # выбранный вручную микрофон не подключён: его название
        self.fallback = None        # …и пока его нет, слушаем этот (название устройства macOS по умолчанию);
                                    # None при missing — не слушаем вовсе (audio_in.fallback = "wait")
        self.on_missing = None      # функция (выбранный, запасной или None): выбранный микрофон пропал
        self.quiet_pids = None      # функция: процессы, чей звук микрофон не глушит (сигнал «фраза напечатана»)
        self._out_busy = False      # динамики заняты другим процессом (обновляет _watch_output)
        self._muted = False
        self.mute_reason = None   # почему глухой: play — играет звук, voice — говорит голос (для окна)
        self._held = False
        self._reset = False       # сбросить незаконченную фразу
        # «слушать после сигнала»: микрофон работает, но звук не распознаём (0 — распознаём как обычно)
        self.deaf = 0
        self._deaf_at = 0.0
        self._deaf_seq = 0
        self._deaf_lock = threading.Lock()
        self.remote = False       # звук идёт с планшета (Goblin Remote): микрофон Мака закрыт полностью
        self._remote_buf = np.zeros(0, dtype=np.float32)
        self._remote_lock = threading.Lock()      # звук телефона приходит из потоков HTTP — режем по одному
        self._frames = queue.Queue(maxsize=FRAMES_MAX)
        self._dropped = 0           # сколько кадров выброшено из переполненной очереди (для журнала)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        self.audio_diagnostics.close()

    def reset(self):
        """Забыть недослушанную фразу (после live-режима)."""
        self._reset = True

    def ear_close(self):
        """Не распознавать звук до ear_open (микрофон при этом работает). Возвращает номер глухоты."""
        with self._deaf_lock:
            self._deaf_seq += 1
            self._deaf_at = time.time()
            self.deaf = self._deaf_seq
            return self.deaf

    def ear_open(self, token=None):
        """Снова распознавать. token — номер из ear_close (чужую глухоту не снимаем); None — снять любую."""
        with self._deaf_lock:
            if self.deaf and (token is None or token == self.deaf):
                self.deaf = 0
                self._reset = True    # забываем то, что звучало до сигнала

    def _callback(self, indata, frames, time_info, status):
        if self.remote:
            return                # слушаем планшет, а не Мак
        self._process(indata[:, 0].copy())

    def _barge_now(self):
        """Речь поверх голоса в наушниках («любая диктовка»): голос замолкает. True — оборвали."""
        if not (self.barge and self.echo_free and self.echo_free()):
            return False
        self._barge_active = bool(self.on_barge()) if self.on_barge else True
        if self._barge_active and self._sid:
            self._interrupted_sids.add(self._sid)
        return True

    def _short_barge(self, speech, threshold):
        if not (self._barge_active and self.short_sound_stop and self.short_sound_stop()):
            return False
        active = [i for i, frame in enumerate(speech)
                  if float(np.sqrt(np.mean(frame ** 2))) >= threshold * .7]
        return bool(active) and (active[-1] - active[0] + 1) * FRAME_MS <= 400

    def finish_for_submit(self, callback):
        """FIFO boundary: close audio already received, then enqueue the submit barrier."""
        with self._remote_lock:
            if self.remote and len(self._remote_buf):
                self._put((self._remote_buf.copy(), False))
                self._remote_buf = np.array([], dtype=np.float32)
            self._frames.put((callback, None), timeout=2)

    def feed_remote(self, samples):
        """Звук с планшета (float32, 16 кГц): режем на кадры по 30 мс и ведём тем же путём, что микрофон."""
        with self._remote_lock:
            buf = np.concatenate([self._remote_buf, samples.astype(np.float32)])
            n = len(buf) // FRAME * FRAME
            for i in range(0, n, FRAME):
                self._process(buf[i:i + FRAME], remote=True)
            self._remote_buf = buf[n:]

    def _put(self, item):
        """Кадр в очередь разбора; переполнена (разбор стоит) — выбросить самый старый, не копить память."""
        while True:
            try:
                self._frames.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._frames.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass

    def _process(self, samples, remote=False):
        if not self.paused or remote:
            self.audio_diagnostics.feed(samples, "phone" if remote else self.diagnostic_input_name)
        tap = self.tap
        if self.hush:
            if tap:
                tap(np.zeros_like(samples))
            self.level = 0.0
            self._reset = True     # после паузы начинаем с чистой фразы
            return
        if self.paused and not remote:
            # микрофон выключен — никуда ничего не идёт (в OpenAI — тишина)
            if tap:
                tap(np.zeros_like(samples))
            self.level = 0.0
            return
        if self.deaf:
            if time.time() - self._deaf_at > DEAF_MAX_S:
                self.ear_open()            # сигнал так и не прозвучал — не глохнем навсегда
            else:
                # «слушать после сигнала»: в распознавание звук не идёт (в OpenAI — тишина)
                if tap:
                    tap(np.zeros_like(samples))
                self.level = float(np.sqrt(np.mean(samples ** 2)))
                return
        speaking = bool(self.hold and self.hold())
        # «выключать микрофон, когда играет звук»: любой звук из динамиков, наш голос — тоже
        playing = self.mute_on_play and (self._out_busy or bool(self.self_playing and self.self_playing()))

        # говорит голос и включено «выключать микрофон» — не слушаем вообще
        if (speaking and self.mute_on_speech) or playing:
            if tap:
                tap(np.zeros_like(samples))
            self.level = 0.0
            if not self._muted:
                self.log.debug("Microphone muted: %s", "sound playing" if playing else "speech playing")
            self._muted = True
            self.mute_reason = "play" if playing else "voice"
            return
        if self._muted:
            self.log.debug("Microphone listening again")
            self._muted = False
            self.mute_reason = None
            self._reset = True     # забываем обрывки, пойманные вокруг голоса

        if tap:
            self.level = float(np.sqrt(np.mean(samples ** 2)))   # режим OpenAI: звук не идёт в run(), шкалу считаем здесь
        held = bool(tap and speaking)

        if tap and not held:
            if self._held:
                self._reset = True     # голос замолчал — забываем, что слушали сами
            tap(samples)
        else:
            if tap:
                tap(np.zeros_like(samples))   # OpenAI получает тишину
            self._put((samples, speaking))
        self._held = held

    # ---------- потоковый распознаватель (диктовка «Apple»): звук фразы идёт в него по ходу речи

    def _stream_start(self, frames):
        stream = self.stream
        self._sid = stream.start(np.concatenate(frames)) if stream else None
        if self._sid and self._barge_active:
            self._interrupted_sids.add(self._sid)
        if self._sid and self.on_start:
            self.on_start(self._sid)

    def _stream_end(self):
        """Фраза кончилась (или режется): id для окончательного текста; None — потока не было."""
        sid, self._sid = self._sid, None
        stream = self.stream
        return stream.end() if (sid and stream) else None

    def _stream_drop(self):
        """Фразу забыли (сброс, слишком короткая): её текст не нужен."""
        sid = self._stream_end()
        if sid and self.stream:
            self.stream.discard(sid)
        if sid and self.on_drop:
            self.on_drop(sid)        # напечатанное по ходу этой фразы так и остаётся — печать следующей не ждёт её итога

    def _watch_output(self):
        """Раз в OUTPUT_CHECK_S: занятые ли динамики (не в звуковом потоке — тот трогать нельзя)."""
        me = os.getpid()
        while not self._stop.is_set():
            skip = {me} | (self.quiet_pids() if self.quiet_pids else set())
            busy = bool(self.mute_on_play and audio_dev.playing(skip))
            if busy != self._out_busy:
                self.log.debug("Speakers %s; microphone %s", *(("busy", "muted") if busy else ("idle", "listening")))
            self._out_busy = busy
            time.sleep(OUTPUT_CHECK_S)

    def run(self):
        """Поток микрофона живёт до stop(): любой сбой (PortAudio, устройство занято, неверная настройка) —
        в журнал, поток звука закрыт, пауза и всё заново. Иначе Voice «жив», а микрофон мёртв навсегда."""
        threading.Thread(target=self._watch_output, daemon=True).start()
        fails = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._listen()
                return                          # stop()
            except Exception as e:
                self.capture_open = False
                if time.time() - started > 60:
                    fails = 0                   # долго работал — это новый сбой, а не падение подряд
                pause = RESTART_S[min(fails, len(RESTART_S) - 1)]
                if fails:
                    self.log.warning("Microphone failed again (%s); retrying in %d s", e, pause)
                else:
                    self.log.exception("Microphone stream failed; retrying in %d s", pause)
                fails += 1
                self.in_speech = False
                self.level = 0.0
                try:
                    self._stream_drop()
                except Exception:
                    pass
                if isinstance(e, sd.PortAudioError):
                    self._reinit()              # «Invalid stream pointer» и т.п. — PortAudio начать с чистого листа
                self._stop.wait(pause)

    def _reinit(self):
        """PortAudio помнит список устройств с запуска — перечитать (OpenAI-голос сам переоткроет свой поток)."""
        self._reinit_at = time.time()
        with audio_out.pa_lock():
            sd._terminate()
            sd._initialize()

    def _note_missing(self, wanted, fallback):
        """Выбранный микрофон пропал: один раз — в журнал и наружу (окно, сигнал, телефон)."""
        if self.missing == wanted and self.fallback == fallback:
            return
        if fallback:
            self.log.warning("Audio: microphone “%s” is disconnected; using “%s”", wanted, fallback)
        else:
            self.log.warning("Audio: waiting for microphone “%s”; listening is paused", wanted)
        self.missing, self.fallback = wanted, fallback
        if self.on_missing:
            try:
                self.on_missing(wanted, fallback)
            except Exception as e:
                self.log.warning("Audio: %s", e)

    def _auto_reset(self):
        """Другой микрофон (или телефон вместо Мака) — громкость другая: подстраиваемся заново."""
        if self._auto:
            self.log.debug("Microphone changed; recalibrating input volume")
        self._auto.clear()
        self.auto_level = 0.0

    def _auto_learn(self, frames, threshold):
        """Фраза без голоса озвучки: её громкость — в автоподстройку минимальной громкости речи."""
        if not self.cfg.get("auto_level"):
            return
        loud = [r for r in (float(np.sqrt(np.mean(f ** 2))) for f in frames) if r > threshold]
        if not loud:
            return
        self._auto.append(float(np.median(loud)))
        if len(self._auto) < AUTO_START:
            return
        speech = float(np.median(self._auto))
        level = min(AUTO_CEIL, max(AUTO_FLOOR, speech * AUTO_SHARE))
        if not self.auto_level or abs(level - self.auto_level) > self.auto_level * 0.1:
            self.log.debug("Microphone auto calibration: minimum speech level %.3f (speech %.3f, phrases %d)",
                           level, speech, len(self._auto))
        self.auto_level = level

    def _listen(self):
        noise = self.cfg["min_level"] / 2  # плавающий уровень фонового шума
        preroll = deque(maxlen=PREROLL_FRAMES)
        speech = []                        # кадры текущей фразы
        barge_run_ms = 0
        loud_run = 0                       # сколько громких кадров подряд
        voiced = 0                         # громких кадров в текущей фразе (длина самой речи, без тишины и preroll)
        quiet_run = 0                      # сколько тихих кадров подряд в речи
        was_long = False                   # фраза уже резалась на куски
        spoken = False                     # во время фразы звучал наш голос
        barged = False                     # фраза оборвала голос (наушники): дальше она обычная
        overlap = 1000 // FRAME_MS         # 1 с перекрытия между кусками
        quiet_before = float("inf")
        leading_pause = float("inf")
        pending_pause = float("inf")

        while not self._stop.is_set():
            devices = audio_dev.defaults()
            ids = audio_dev.device_ids()        # список устройств CoreAudio: изменился — подключили / отключили
            changed = False
            next_check = time.time() + DEVICE_CHECK_S
            next_missing = time.time() + MISSING_CHECK_S
            wanted = self.input_name
            device = audio_out.index(wanted, "in")
            if wanted and device is None and not self.remote and not audio_out.fallback_in():
                # выбранный микрофон не подключён, а подмена выключена (audio_in.fallback = "wait"): не слушаем и ждём
                self._note_missing(wanted, None)
                self._stream_drop()
                speech, was_long, spoken, quiet_run, loud_run, voiced = [], False, False, 0, 0, 0
                preroll.clear()
                self.in_speech = False
                self.level = 0.0
                # ждём, пока что-то изменится: список устройств CoreAudio, устройство по умолчанию, выбор в окне.
                # PortAudio перечитываем только тогда (раньше — каждые 5 с: это рвало голос OpenAI и запись тестов)
                while not self._stop.is_set() and self.input_name == wanted and not self.remote \
                        and audio_dev.defaults() == devices and audio_dev.device_ids() == ids \
                        and (ids is not None or time.time() - getattr(self, "_reinit_at", 0) < REINIT_MIN_S):
                    time.sleep(0.5)
                if self.input_name == wanted and not self.remote and not self._stop.is_set():
                    self._reinit()               # вдруг его подключили
                continue
            if wanted and device is None and not self.remote:
                # выбранный микрофон не подключён — слушаем микрофон macOS по умолчанию, пока его нет
                self._note_missing(wanted, audio_dev.names()[0] or "macOS default")
            elif not wanted or device is not None:
                if self.missing and wanted:
                    self.log.info("Audio: microphone “%s” connected and listening", wanted)
                self.missing, self.fallback = None, None
            # иначе слушаем телефон, а выбранного микрофона нет — пометку о пропаже не снимаем
            # открыть / закрыть — в общей очереди со звуками (audio_out.pa_lock): иначе CoreAudio может зависнуть
            with audio_out.Locked(lambda: sd.InputStream(samplerate=RATE, channels=1, dtype="float32", device=device,
                                                         blocksize=FRAME, callback=self._callback)) as stream:
                self.capture_open = True
                if self.on_input_device:
                    self.diagnostic_input_name = wanted if device is not None else audio_dev.names()[0]
                    self.on_input_device(self.diagnostic_input_name)
                    noise = self.cfg["min_level"] / 2
                self.log.info(t("log.mic_open"))
                while not self._stop.is_set():
                    # сменили микрофон или динамики в macOS — переоткрыть поток на новом устройстве
                    # (и когда выбранный вручную микрофон не найден: его подключили — список нужно перечитать)
                    if time.time() >= next_check:
                        next_check = time.time() + DEVICE_CHECK_S
                        now = audio_dev.defaults()
                        if now and devices and now != devices and (not wanted or device is None):
                            self.log.info("Audio device changed; reopening the microphone")
                            changed = True
                            break
                    if wanted and device is None and not self.remote:   # выбранного микрофона нет: слушаем запасной
                        if not audio_out.fallback_in():
                            changed = True               # подмена выключена — закрыть и ждать выбранный
                            break
                        if self.missing != wanted:       # телефон отпустил — пропажа выбранного видна только теперь
                            self._note_missing(wanted, audio_dev.names()[0] or "macOS default")
                    if wanted and not self.remote and time.time() >= next_missing:
                        # выбранный вручную: подключили / отключили что-то — его могли вернуть (слушаем запасной)
                        # или выдернуть (поток на пропавшем устройстве молчит) — открыть заново
                        next_missing = time.time() + MISSING_CHECK_S
                        if audio_dev.device_ids() != ids or (device is None and ids is None and
                                                             time.time() - getattr(self, "_reinit_at", 0) >= REINIT_MIN_S):
                            self.log.info("Audio device list changed: %s “%s”",
                                          "looking for" if device is None else "checking", wanted)
                            changed = True
                            break
                    if self.input_name != wanted:                    # в окне выбрали другой микрофон
                        self.log.info("Audio: switching to microphone “%s”", self.input_name or "macOS default")
                        changed = True
                        break
                    # планшет в режиме «Голос» — микрофон Мака выключаем совсем (и значок микрофона гаснет)
                    if self.remote and stream.active:
                        with audio_out.pa_lock():
                            stream.stop()
                        self._reset = True
                        self._auto_reset()
                    elif not self.remote and stream.stopped:
                        with audio_out.pa_lock():
                            stream.start()
                        self._reset = True
                        self._auto_reset()
                    try:
                        frame, speaking = self._frames.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if callable(frame):
                        try:
                            if speech:
                                sid = self._stream_end()
                                self.on_phrase(to_wav(np.concatenate(speech), leading_pause, self._barge_active),
                                               False, spoken, sid=sid)
                            else:
                                self._stream_drop()
                        finally:
                            speech, was_long, spoken, quiet_run, loud_run, voiced = [], False, False, 0, 0, 0
                            preroll.clear()
                            self.in_speech = False
                            self._barge_active = False
                            quiet_before = 0
                            frame()
                        continue

                    if (self.paused and not self.remote) or self._reset or self.deaf:   # планшет слышен и при выключенном микрофоне Мака
                        self._stream_drop()
                        self._reset = False
                        self._barge_active = False
                        speech, was_long, spoken, quiet_run, loud_run, voiced = [], False, False, 0, 0, 0
                        preroll.clear()
                        self.in_speech = False
                        quiet_before = 0
                        continue

                    # настройки читаем каждый кадр — их можно менять на ходу
                    min_level = self.cfg["min_level"]
                    if self.cfg.get("auto_level") and self.auto_level:
                        min_level = self.auto_level
                    start_ratio = self.cfg["start_ratio"]
                    silence_frames = self.cfg["silence_ms"] // FRAME_MS
                    min_frames = self.cfg["min_speech_ms"] // FRAME_MS
                    max_s = self.cfg.get("dictation_max_s", 30) if self.dictation else self.cfg["max_speech_s"]
                    if self.dictation and self.stream:
                        # потоковая диктовка печатает по ходу — резать часто незачем (Apple на обрыве теряет текст)
                        max_s = max(max_s, self.cfg.get("stream_max_s", 50))
                    if spoken or speaking:
                        # поверх голоса — короткие куски: команду «замолчать» слышно сразу, а не в конце озвучки
                        max_s = min(max_s, self.cfg.get("speaking_chunk_s", 3))
                    max_frames = int(max_s * 1000 / FRAME_MS)

                    rms = float(np.sqrt(np.mean(frame ** 2)))
                    self.level = rms
                    threshold = max(min_level, noise * start_ratio) if self.cfg.get("noise_adaptation", True) else min_level
                    self.threshold = threshold
                    barge_run_ms = barge_run_ms + FRAME_MS if speaking and rms > threshold else 0
                    if rms > threshold:
                        if not speech and not loud_run:
                            pending_pause = quiet_before * FRAME_MS / 1000
                        quiet_before = 0
                    elif rms < threshold * 0.7:
                        quiet_before += 1
                    else:
                        quiet_before = 0

                    if speech and rms > threshold:
                        self.last_voice = time.time()

                    # голос в наушниках + «любая диктовка»: речь поверх голоса обрывает его сразу, фраза — обычная
                    if barged:
                        speaking = False
                    elif speaking and speech and barge_run_ms >= 100 and self._barge_now():
                        barged, speaking, spoken = True, False, False

                    # ---- тишина: ждём начала речи
                    if not speech:
                        barged = False
                        self._barge_active = False
                        preroll.append(frame)
                        if rms > threshold:
                            loud_run += 1
                            if loud_run >= 2:
                                speech = list(preroll)
                                self._probe_at = self.probe_ms[0]
                                leading_pause = pending_pause
                                voiced = loud_run
                                quiet_run = 0
                                if speaking and barge_run_ms >= 100 and self._barge_now():
                                    barged, speaking = True, False
                                self._stream_start(speech)
                                spoken = speaking
                                self.in_speech = True
                                self.last_voice = time.time()
                        else:
                            loud_run = 0
                            noise = noise * 0.98 + rms * 0.02   # подстраиваемся под фон
                        continue

                    # ---- идёт речь
                    if spoken and not speaking:
                        # голос замолчал посреди фразы: кусок поверх голоса — отдельно (команды / «любая диктовка»),
                        # а дальше — новая обычная фраза, её можно печатать (иначе напечатанное сотрётся)
                        sid = self._stream_end()
                        if voiced >= min_frames:
                            self.on_phrase(to_wav(np.concatenate(speech), leading_pause, self._barge_active), not self.barge, True, sid=sid)
                        elif sid and self.stream:
                            self.stream.discard(sid)
                            if self.on_drop:
                                self.on_drop(sid)
                        # последние 0,75 с переносим, только если речь поверх голоса печатается («любая диктовка»):
                        # иначе туда попадает хвост озвучки из колонок, а перебить голос диктовкой всё равно нельзя
                        speech = (speech[-AFTER_VOICE_FRAMES:] if self.barge else []) + [frame]
                        voiced = int(rms > threshold)
                        self._stream_start(speech)
                        spoken, was_long, quiet_run = False, False, 0
                        leading_pause = 0
                        self._probe_at = self.probe_ms[0]
                        continue
                    speech.append(frame)
                    # «дружище»: начало фразы — на проверку слова-ключа каждые 0,3 с, не дожидаясь её конца (колокольчик сразу)
                    if self.on_probe and self._probe_at and len(speech) * FRAME_MS >= self._probe_at and not (self._barge_active and self.short_sound_stop and self.short_sound_stop()):
                        _, step, last = self.probe_ms
                        self._probe_at = self._probe_at + step if self._probe_at + step <= last else 0
                        if self.on_probe(to_wav(np.concatenate(speech), leading_pause, self._barge_active)) is False:
                            self._probe_at = 0
                    if self._sid and self.stream:
                        self.stream.feed(frame)
                    spoken = spoken or speaking
                    quiet_run = quiet_run + 1 if rms < threshold * 0.7 else 0
                    if rms > threshold:
                        voiced += 1

                    if len(speech) > max_frames:
                        sid = self._stream_end()
                        if self.dictation and (not spoken or self.barge):
                            # диктовка (и «любая диктовка» поверх голоса): кусок целиком, без повторов;
                            # без потока — режем в паузе, хвост после неё начинает следующий кусок (слово не рвётся)
                            cut = len(speech) if sid else quiet_cut(speech)
                            if not spoken and not barged:
                                self._auto_learn(speech[:cut], threshold)
                            self.on_phrase(to_wav(np.concatenate(speech[:cut]), leading_pause, self._barge_active), False, spoken, final=False, sid=sid)
                            self._barge_active = False
                            leading_pause = 0
                            speech = speech[cut:]
                            voiced = sum(1 for f in speech if float(np.sqrt(np.mean(f ** 2))) > threshold)
                        else:
                            # длинная речь: кусок только для «заткнись», продолжаем с перекрытием
                            self.on_phrase(to_wav(np.concatenate(speech), leading_pause, self._barge_active), True, spoken, final=False, sid=sid)
                            leading_pause = 0
                            speech = speech[-overlap:]
                            was_long = True
                            self._stream_start(speech)
                        spoken = speaking

                    if quiet_run >= silence_frames:
                        # конец фразы
                        # короткое прерывание проверяется до передачи фразы распознавателю
                        if self._short_barge(speech, threshold):
                            self._stream_drop()
                            self.log.info("Short sound interrupted speech; no dictation")
                        elif voiced >= min_frames:
                            sid = self._stream_end()
                            if not spoken and not barged:      # во время озвучки в микрофоне эхо — не учимся
                                self._auto_learn(speech, threshold)
                            self.on_phrase(to_wav(np.concatenate(speech), leading_pause, self._barge_active), was_long, spoken, sid=sid)
                        elif 0 < (weak_ms := self.cfg.get("min_command_ms", WEAK_MS)) <= voiced * FRAME_MS \
                                and len(speech) * FRAME_MS <= WEAK_MAX_MS and not spoken:   # 0 — выключено
                            # короткое тихое слово: не печатаем, только команда («Рекс» отдельно после фразы)
                            self._stream_drop()
                            self.log.debug("Speech detection: short %.2f s (%d ms above threshold); command only",
                                           len(speech) * FRAME_MS / 1000, voiced * FRAME_MS)
                            self.on_phrase(to_wav(np.concatenate(speech), leading_pause, self._barge_active), False, False, weak=True)
                        else:
                            # отброшено: пишем в журнал, чтобы видеть потерянные короткие слова
                            peak = max(float(np.sqrt(np.mean(f ** 2))) for f in speech)
                            self.log.debug("Speech detection: skipped %.2f s (%d ms above threshold < %d ms, peak %.3f, threshold %.3f)",
                                           len(speech) * FRAME_MS / 1000, voiced * FRAME_MS,
                                           min_frames * FRAME_MS, peak, threshold)
                            self._stream_drop()
                        speech = []
                        preroll.clear()
                        loud_run = 0
                        voiced = 0
                        quiet_run = 0
                        was_long = False
                        spoken = False
                        self.in_speech = False

            self.capture_open = False
            if not changed:
                break
            if self._dropped:
                self.log.debug("Microphone processing fell behind; dropped %d frames", self._dropped)
                self._dropped = 0
            self._reinit()          # PortAudio помнит список устройств с запуска — перечитать
            self._auto_reset()
            self._stream_drop()
            speech, was_long, spoken, quiet_run, loud_run, voiced = [], False, False, 0, 0, 0
            preroll.clear()
            self.in_speech = False
