"""Пульт с tabletа через Goblin Remote: звук tabletа вместо микрофона Мака, озвучка — обратно на tablet.

Связь только локальная (127.0.0.1, сервер окна): мост Goblin Remote (voice_bridge.py) раз в секунду сообщает
«tablet в режиме Голос» (POST /api/remote/state), шлёт звук (POST /api/remote/audio — PCM 16 бит, 16 кГц, моно)
и забирает то, что надо проиграть на tabletе (GET /api/remote/out?after=N&wait=S).

Пока tablet в режиме — работает ЛИБО tablet, ЛИБО computer: микрофон Мака выключен полностью
(поток закрыт), голос и сигналы на Маке не звучат — уходят на tablet. Пропала связь с мостом
дольше ALIVE_SEC — всё возвращается на computer.
"""

import base64
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np

ALIVE_SEC = 12         # без вестей от моста дольше — tablet считается отключённым (хостинг отвечает до ~2 с)
KEEP = 40              # сколько последних отправок помнить
TMP = Path(tempfile.gettempdir()) / "goblin-voice"
# сигналы, для которых у телефона есть свой звук (assets/sounds, VoiceService.prefetchSounds); остальные — замена
PHONE_SIGNALS = {"space", "enter", "text", "clear", "switch", "mode_on", "mode_off", "error"}
PHONE_ALIAS = {"jonny": "switch"}      # «Джони» / «Амиго»: ввод переключён — как переключение вкладки
# режимы «Голоса» на телефоне — подписи для журнала (как в GLOSSARY); в протоколе значения прежние: stream / hold / text
PHONE_MODE_NAMES = {"stream": "Streaming", "hold": "Hold to talk", "text": "Recognize on phone"}


def _render(build, suffix):
    """build(путь) → команда (say/afconvert) с выводом в этот файл. → (байты, длительность с) или (None, 0)."""
    TMP.mkdir(exist_ok=True)
    out = TMP / f"{time.time_ns()}{suffix}"
    try:
        subprocess.run(build(str(out)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        info = subprocess.run(["afinfo", str(out)], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"estimated duration: ([\d.]+)", info)
        return out.read_bytes(), float(m.group(1)) if m else 0.0
    except Exception:
        return None, 0.0
    finally:
        out.unlink(missing_ok=True)


class Remote:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.ready = threading.Event()   # есть новое для tabletа
        self.seen = 0.0                  # когда мост последний раз сообщал «tablet в режиме»
        self.reported = False            # что последним сообщил мост (как есть, без учёта taken)
        self.taken = False               # Мак забрал микрофон кнопкой «Вкл»: tablet не слушаем, пока он
                                         # не выключит «Голос» и не включит его снова
        self.mode = "stream"             # режим микрофона tabletа: stream / hold / text
        self.reply = "text"              # озвучка: text — читает голос самого tabletа,
                                         # mac — звучит на самом Маке (микрофон при этом — tablet)
        self.out = []                    # [{id, kind, ...}]
        self.next_id = 0
        self.until = 0.0                 # до какого времени tablet, по нашим расчётам, ещё говорит
        self.sounds = {}                 # кэш сигналов в m4a
        self.on_change = None            # (active, reason): tablet включил / выключил «Голос»; reason —
                                         # bridge (так сообщил мост), timeout (мост пропал), taken (забрал Мак)
        # блок «Звук»: mic и reply задаёт Voice (настройка sound), а не страница. mic — чей микрофон слушаем:
        # tablet (sound.mic = phone) / mac / off; reply — где звучат ответы: text (телефон) / mac
        self.mic = "tablet"
        self.mic_dev = self.out_dev = ""   # гарнитура / динамик телефона (мост, из диагностики приложения)
        self.reported_mic = None         # что последним сообщила страница (кнопки старого приложения 1.2.1)
        self.reported_reply = None
        self.on_mic = None               # (mic) страница сама сменила микрофон: tablet / mac / off
        self.on_reply = None             # (reply) страница сама сменила, где звучат ответы: text / mac
        self.echo = lambda: True         # защита от эха сейчас работает (Voice: echo_now)
        # страница знает блок «Звук» (1.3.0+: прислала config sound / phone_output или proto ≥ 2 в состоянии):
        # её mic / reply в сердцебиении — только факт (пишет ли телефон), в настройку их не переводим
        self.knows_sound = False
        self.android_input = False
        self.channels = {}
        self.channels_seen = 0.0
        self.channel_requests = {}
        self.feed = None                 # куда отдавать звук tabletа (Listener.feed_remote)

    # ---------- состояние

    def channel_state(self):
        with self.lock:
            return dict(self.channels) if time.time() - self.channels_seen < ALIVE_SEC else {"devices": []}

    def update_channels(self, channels):
        if not isinstance(channels, dict):
            return
        devices = []
        for d in (channels.get("devices") or [])[:64]:
            if isinstance(d, dict) and isinstance(d.get("id"), str) and re.fullmatch(r"[a-z0-9]{8,24}", d["id"]):
                audio = d.get("audio_devices")
                if isinstance(audio, dict):
                    audio = {key: str(audio.get(key) or "")[:80] for key in ("input_headset", "output_headset", "model")} | {
                        key: [n[:80] for n in d["audio_devices"].get(key, [])[:12] if isinstance(n, str)]
                        if isinstance(d["audio_devices"].get(key), list) else [] for key in ("inputs", "outputs")}
                    activity = d["audio_devices"].get("activity")
                    if isinstance(activity, dict):
                        audio["activity"] = {key: activity.get(key) is True for key in ("input", "output")}
                        audio["activity"].update({key: str(activity.get(key) or "")[:80] for key in ("input_name", "output_name")})
                    raw_buttons = d["audio_devices"].get("buttons")
                    audio["buttons"] = [{"id": b["id"][:128], "name": b["name"][:80]} for b in raw_buttons[:16]
                                        if isinstance(b, dict) and isinstance(b.get("id"), str) and isinstance(b.get("name"), str)] if isinstance(raw_buttons, list) else []
                else:
                    audio = None
                devices.append({"id": d["id"], "name": str(d.get("name") or "")[:60], "audio_devices": audio})
        with self.lock:
            self.channels = {"devices": devices, **{k: channels.get(k, "") for k in ("input", "output", "video", "revision", "fallback")}}
            self.channels_seen = time.time()

    def select_device(self, kind, device):
        if kind not in ("mic", "speech", "video") or (not device and kind != "video") or (device and not any(d["id"] == device for d in self.channel_state().get("devices", []))):
            raise ValueError("The device is disconnected")
        with self.lock:
            self.channel_requests[kind] = {"id": uuid.uuid4().hex, "kind": kind,
                "channel": {"mic": "input", "speech": "output", "video": "video"}[kind], "target": device, "created": time.time()}

    def pending_channels(self):
        with self.lock:
            self.channel_requests = {k: v for k, v in self.channel_requests.items() if time.time() - v["created"] < 15}
            return list(self.channel_requests.values())

    def finish_channel(self, request_id, ok):
        with self.lock:
            for kind, request in list(self.channel_requests.items()):
                if request["id"] == request_id:
                    del self.channel_requests[kind]
                    return kind if ok and time.time() - request["created"] < 15 else None
        return None

    def cancel_channel(self, kind):
        with self.lock:
            self.channel_requests.pop(kind, None)

    def active(self):
        return time.time() - self.seen < ALIVE_SEC

    def mic_here(self):
        """Слушаем звук tabletа, а микрофон Мака закрыт (tablet в «Голосе» и микрофон — не «на computerе»)."""
        return self.active() and self.mic != "mac"

    def set_state(self, active, reply=None, mode=None, mic=None, proto=None, mic_dev=None, out_dev=None):
        """Сердцебиение моста: страница в «Голосе» или нет, её кнопки микрофона (tablet / mac / off) и ответов.

        Кнопки страницы — только переключатели: Voice берёт их при смене (on_mic / on_reply) и переводит в блок
        «Звук»; что слушать и где звучать, решает настройка sound, а не каждое сообщение страницы."""
        was = self.active()
        # что работает на телефоне (по диагностике приложения): микрофон headset / phone, озвучка headset / speaker
        self.mic_dev = mic_dev if mic_dev in ("headset", "phone") else ""
        self.out_dev = out_dev if out_dev in ("headset", "speaker") else ""
        if isinstance(proto, (int, float)) and not isinstance(proto, bool) and proto >= 2:
            self.knows_sound = True
        mic = mic if mic in ("tablet", "mac", "off") else None
        mic_was = self.reported_mic
        if self.taken and active and not self.reported:
            self.taken = False           # на tabletе заново включили «Голос» — снова его очередь
            self.log.info("tablet: Voice resumed — using the tablet microphone again")
        elif self.taken and active and mic == "tablet" and mic_was == "mac":
            # страница после taken остаётся в «Голосе» и показывает «Мак»; выбрали там «Телефон» — тоже его очередь
            self.taken = False
            self.log.info("tablet: Phone microphone selected — using the tablet microphone again")
        self.reported = bool(active)
        if self.taken:
            active = False
        self.seen = time.time() if active else 0.0
        if mode in ("stream", "hold", "text") and mode != self.mode:
            self.log.info("phone: mode “%s” → “%s”", PHONE_MODE_NAMES[self.mode], PHONE_MODE_NAMES[mode])
        if mode in ("stream", "hold", "text"):
            self.mode = mode             # text («Recognize on phone») — телефон распознаёт сам и шлёт готовый текст (звука нет)
        reply = ("mac" if reply == "mac" else "text") if reply in ("audio", "text", "mac") else None
        if time.time() < getattr(self, "reply_wait", 0):
            reply = None                 # выбор с Мака ещё не дошёл до tabletа — его старое значение не берём
        reply_was = self.reported_reply
        if not was:
            mic_was = reply_was = None   # кнопки страницы до ухода из «Голоса» не в счёт — только свежие
        self.reported_mic = mic or (mic_was if was else None)
        self.reported_reply = reply or (reply_was if was else None)
        if was != self.active():
            self.log.info("tablet: %s", "Voice mode enabled" if active else "Voice mode disabled")
            if self.on_change:
                self.on_change(self.active(), "bridge")
            return
        if not self.active():
            return
        if self.knows_sound:
            return                       # новая страница: настройку шлёт сама (config sound), тут — только факт
        # кнопки старой страницы (1.2.1). «Выкл» не переводим: так же сообщается «телефон сейчас не пишет»
        if mic and mic_was and mic != mic_was and mic != "off":
            self.log.info("tablet: microphone → %s", {"tablet": "tablet", "mac": "computer"}[mic])
            if self.on_mic:
                self.on_mic(mic)
        if reply and reply_was and reply != reply_was:
            self.log.debug("tablet: replies %s → %s", reply_was, reply)
            if self.on_reply:
                self.on_reply(reply)

    def set_reply(self, reply):
        """Озвучка при «Голосе» с tabletа: text — tablet speech, mac — audio on the Mac (выбор из окна Voice).
        Планшет получает команду и сам переключается (то же, что кнопки на странице)."""
        if reply not in ("text", "mac"):
            raise ValueError(reply)
        self.reply = reply
        self.reply_wait = time.time() + 6
        self.reported_reply = reply
        self._push({"kind": "reply", "value": reply})
        self.log.info("tablet: speech → %s (from Voice)", "tablet speech" if reply == "text" else "audio on the Mac")

    def mic_to_mac(self, source="mac"):
        """Микрофон теперь у Мака (блок «Звук»: Микрофон — Мак): страница выключает свой микрофон и показывает «Мак».
        Событие то же, что прежнее taken, но «Голос» телефона остаётся включён: озвучка может звучать на нём."""
        self.reported_mic = "mac"
        self._push({"kind": "taken", "mic": "mac", "source": source if source in ("mac", "phone") else "mac"})

    def plays_here(self):
        """Озвучка и сигналы — на tablet (а не в динамики Мака)."""
        return self.active() and self.reply != "mac"

    def tick(self):
        """Раз в секунду: мост пропал — возвращаем всё на computer."""
        if self.seen and not self.active():
            self.seen = 0.0
            self.log.info("tablet: bridge disconnected — audio returns to the computer")
            if self.on_change:
                self.on_change(False, "timeout")    # обрыв связи — не то же, что телефон сам вышел из «Голоса»

    def take_back(self, source="mac"):
        """«Вкл» на Маке: микрофон и звук — снова computerу, tablet выключает у себя «Голос».

        Планшету — событие {"kind": "taken", "mic": "mac", "source": "mac" | "phone"}: микрофон теперь у Мака
        (source — где нажали «Вкл»: in Voice или кнопкой «Микрофон Мака» на самом телефоне). Это только про
        микрофон: вид страницы («Голос» / «Экран») оно не задаёт."""
        was = self.active()
        self.taken = True
        self.seen = 0.0
        self.until = 0.0
        self._push({"kind": "taken", "mic": "mac", "source": source if source in ("mac", "phone") else "mac"})
        self.log.info("tablet: the computer took the microphone (%s) — turning off Voice on the tablet",
                      "using the phone button" if source == "phone" else "in Voice")
        if was and self.on_change:
            self.on_change(False, "taken")

    # ---------- звук с tabletа

    def audio(self, pcm16):
        if not self.active() or self.feed is None or not pcm16:
            return
        if self.mic != "tablet":
            return                       # микрофон «Мак» / «Выкл»: запоздавшие куски tabletа не слушаем
        samples = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
        self.feed(samples)

    # ---------- то, что звучит на tabletе

    def _push(self, item):
        with self.lock:
            self.next_id += 1
            item["id"] = self.next_id
            self.out.append(item)
            self.out = self.out[-KEEP:]
        self.ready.set()

    def take(self, after, wait=0.0):
        """Отправки новее after (ждём до wait секунд, если их нет)."""
        end = time.time() + max(0.0, min(wait, 15.0))
        while True:
            with self.lock:
                items = [i for i in self.out if i["id"] > after]
                if items or time.time() >= end:
                    self.ready.clear()
                    return {"items": items, "last": self.next_id}
                # нового нет — сбросить старый сигнал до ожидания (иначе wait возвращается сразу и цикл крутится
                # вхолостую); _push кладёт под этой же блокировкой, так что новое не проскочит
                self.ready.clear()
            self.ready.wait(max(0.0, min(0.5, end - time.time())))

    def busy(self):
        """Планшет, по расчётам, ещё говорит — микрофон tabletа в это время не слушаем (эхо)."""
        return time.time() < self.until

    def speak(self, text, voice, rate):
        """Фраза на tablet — текстом: читает голос самого tabletа (звук с Мака не передаётся)."""
        self.until = time.time() + len(text) / 14 + 0.5              # ~14 знаков в секунду
        self._push({"kind": "text", "text": text, "voice": voice, "rate": rate})

    def speak_audio(self, data, mime, seconds):
        """Готовый звук озвучки (Silero / Grok с Мака) — на телефон; куски идут друг за другом."""
        self.until = max(self.until, time.time()) + seconds + 0.5
        self._push({"kind": "audio", "data": base64.b64encode(data).decode(), "mime": mime, "volume": 1.0})

    def sound_file(self, data, mime, seconds, volume=1.0, echo=True):
        """Звук из файла (прослушать сигнал Мака на телефоне) — как кусок озвучки, со своей громкостью.
        echo=False — не считать телефон «говорящим» (колокольчик посреди фразы не должен выбросить её как эхо)."""
        if echo:
            self.until = max(self.until, time.time()) + seconds + 0.3
        self._push({"kind": "audio", "data": base64.b64encode(data).decode(), "mime": mime, "volume": volume})

    def signal(self, name, volume=1.0):
        """Сигнал (пробел, Enter, напечатано, переключено…) на tablet — только название:
        звук tablet синтезирует сам, файлы не пересылаются."""
        name = PHONE_ALIAS.get(name, name)
        if name not in PHONE_SIGNALS:
            name = "switch"                      # у телефона нет такого звука — лучше знакомый сигнал, чем тишина
        self._push({"kind": "signal", "name": name, "volume": volume})

    def said(self, text):
        """Что напечатано с голоса tabletа — в ленту чата на tabletе (вопрос пользователя)."""
        if self.active() and text.strip():
            self._push({"kind": "said", "text": text.strip()[:4000]})

    def command(self, text):
        """Выполненная команда (Enter, переключение терминала…) — строкой в ленту чата на tabletе."""
        if self.active() and text.strip():
            self._push({"kind": "cmd", "text": text.strip()[:200]})

    def answer(self, agent, text):
        """Текст ответа агента — показать на tabletе (вслух — отдельно, по настройке «Читать ответы»)."""
        if self.active():
            self._push({"kind": "answer", "agent": agent, "text": text[:20000]})

    def mac_speaking(self, on):
        """«Звук на Маке»: Мак заговорил / замолчал — tablet на это время глушит своё распознавание (эхо из колонок)."""
        # «замолчал» шлём всегда: озвучку могли переключить на tablet посреди речи Мака — иначе пауза навсегда
        # в наушниках Мака (эхо «авто» — защиты нет) телефон не глохнет: перебивать можно
        # «замолчал» — и неактивному телефону: он вышел из «Голоса» посреди речи Мака — пауза не должна остаться (28.10)
        if not on or (self.active() and self.reply == "mac" and self.echo()):
            self._push({"kind": "mac_speaking", "on": bool(on)})

    def hush(self):
        """«Заткнись»: tablet прерывает озвучку."""
        self.until = 0.0
        self._push({"kind": "stop"})
