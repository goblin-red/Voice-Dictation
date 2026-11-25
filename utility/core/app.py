"""GOBL(in) Voice — окно-пульт голосового управления вкладками Orca.

Два режима распознавания (тумблер в окне, при переключении — короткий звук):
  штатный — Whisper на computerе;
  OpenAI  — OpenAI live в облаке.
Тумблер «Голосовой ввод текста»: включён — речь печатается во вкладку (в обоих режимах),
выключен — работают только команды.
Команды в обоих режимах: «дамбл-бамбл» — пробел, «рекс» — Enter, «заткнись» — прервать голос,
«переключи терминал» — следующая вкладка с агентом в активном проекте.
Модели выбираются в настройках; «выключи динамик» / «включи озвучку» управляют чтением ответов.
Всё уходит во вкладку Orca, которая сейчас в фокусе, — или прямо в терминал (настройка target, direct.py).
Ответы агентов читаются голосом.
Закрыли окно — утилита завершается целиком.
Язык окна, статусов, журнала и фраз голоса — русский или английский (переключатель EN / RU в шапке; главный — английский).
"""

import concurrent.futures
import copy
import json
import zipa_ipa
import voice_tests
import audio_out
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile

import AppKit
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import webview

import audio_dev
import mic_profiles
import direct
import fuzzy
import html_reader
import log_html
import i18n
import orca_cli
import media_controls
import orchestration
from i18n import t
from last_terminal import LastTerminal
from jonny import Jonny
from listener import Listener, PhraseAudio
import live as live_module
from live import LiveSession
from matcher import MODE_COMMANDS, Matcher, expand_parts, word_lang
from remote import Remote
from settings_server import KEY_MASK, SettingsServer
from speaker import AnswerWatcher, Speaker
from whisper_srv import WhisperServer
from local_voice import GigaAM, Silero
from apple_stt import AppleSTT
import grok_voice
import openai_voice
from openai_voice import OpenAISTT, OpenAITTS
from grok_voice import GrokSTT, GrokTTS
from gemini_voice import GeminiSTT
from gemini_tts import GeminiTTS
from subscription_stt import SubscriptionSTT
import gemini_auth
from command_settings import migrate_commands as migrate_voice_commands

HERE = Path(__file__).resolve().parent
from paths import ROOT, DATA, LOGS, ARCHIVE, UI as UI_DIR, SOUNDS as SOUNDS_DIR, TRANSCRIPTION, APP_BUNDLE   # раскладка папок: core/paths.py
CONFIG_FILE = DATA / "config.json"
DEFAULT_CONFIG = DATA / "config.default.json"
LOG_FILE = LOGS / "orca-voice.log"
LOG_HTML = LOGS / "orca-voice.html"   # журнал для браузера (кнопка «открыть файл»)
FULL_LOG = LOGS / "orca-voice-full.log"     # подробный журнал: всё подряд, без лимита строк
FULL_HTML = LOGS / "orca-voice-full.html"   # он же для браузера (кнопка «Расширенный журнал»)
NOISY_LOGGERS = ("aioice", "aiortc", "websockets", "pywebview")   # в обычный журнал — только их предупреждения
LOG_ARCHIVE = ARCHIVE          # журналы прошлых дней: orca-voice-ГГГГ-ММ-ДД.log и .html
PID_FILE = LOGS / "app.pid"
CONFIG_LOCK = threading.RLock()     # config.json и self.cfg меняют окно, кнопки, телефон, ticker — по одному


def write_config(cfg):
    """config.json целиком: во временный файл и подменой (обрыв посреди записи не портит настройки),
    под CONFIG_LOCK. Права 600 — в файле ключи API."""
    with CONFIG_LOCK:
        data = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)

SOUNDS = {
    "recognize_send": str(SOUNDS_DIR / "recognize_send.wav"),
    "gemini_prepare": str(SOUNDS_DIR / "gemini_prepare.wav"),
    "space": "/System/Library/Sounds/Tink.aiff",
    "enter": "/System/Library/Sounds/Pop.aiff",
    "text": str(SOUNDS_DIR / "typed.wav"),   # фраза напечатана: короткий щелчок 60 мс (Purr Whisper слышал как «спасибо»)
    "switch": "/System/Library/Sounds/Morse.aiff",    # вкладка переключена
    "mode_on": "/System/Library/Sounds/Glass.aiff",
    "mode_off": "/System/Library/Sounds/Bottle.aiff",
    "buttons_terminal": "/System/Library/Sounds/Glass.aiff",
    "buttons_standard": "/System/Library/Sounds/Bottle.aiff",
    "buttons_extra1": "/System/Library/Sounds/Ping.aiff",
    "buttons_extra2": "/System/Library/Sounds/Pop.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
    "jonny": "/System/Library/Sounds/Hero.aiff",      # «Джони»: ввод пошёл в его терминал
    "clear": str(SOUNDS_DIR / "erased.wav"),   # «удали фразу»: тихий «смах» 110 мс
    "wake": str(SOUNDS_DIR / "wake_bell.wav"),  # «дружище»: тихий колокольчик «слушаю»
}
COMMAND_SIGNAL_DEFAULTS = {
    "mic_on": "jonny",
    "mic_mute": "mode_off",
    "mic_off": "mode_off",
    "amigo": "mode_off",
    "double": "space",
    "previous": "switch",
    "project": "switch",
    "active": "switch",
    "tab": "switch",
    "read_on": "mode_on",
    "read_off": "mode_off",
}
SOUNDS.update({action: SOUNDS[signal] for action, signal in COMMAND_SIGNAL_DEFAULTS.items()})
SIGNAL_DIR = SOUNDS_DIR   # своя библиотека звуков сигналов (кладите сюда wav, aiff, mp3)
SYSTEM_SOUNDS = Path("/System/Library/Sounds")
SOUND_EXT = (".wav", ".aiff", ".aif", ".mp3", ".m4a", ".caf")
# «агент работает» (п. 31 плана): пока агент думает над вашим запросом — тихий тик раз в work_tick_s
WORK_SOUND = SIGNAL_DIR / "work_soft_tuk.wav"   # мягкий низкий «тук» (выбор владельца 28.10)
WORK_VOLUME = 0.6
WORK_MAX_S = 15 * 60     # столько без ответа — тики прекращаются (ответ потерялся или агент ждёт вас)
WORK_ECHO_MAC = 0.4      # эхо тика из колонок Мака: сверх длины тика он считается нашим голосом столько, с
WORK_AFTER_SPEECH_S = 1.5  # после озвучки тик — не раньше (конец речи на телефоне известен лишь примерно), с
WORK_CLIP_RETRY_S = 600  # звук для телефона не перекодировался — следующая попытка не раньше, с
RELEASE_WAIT = 4.0       # сколько ждать фразу после отпускания кнопки на tabletе, с
ECHO_AFTER_S = 2.5      # фраза начата в эти секунды после голоса — проверить на эхо озвучки
ENTER_AFTER_TEXT = 0.5   # пауза между напечатанным текстом и Enter, с (иначе Enter станет переводом строки)
WHISPER_KEYS = ("model", "language", "prompt", "whisper_port")   # их смена перезапускает Whisper
LANGUAGE_MODES = ("ui", "auto")
DOUBLE_SPACE_PAUSE = 1.0   # «румба»: пауза между двумя пробелами (с)
DICTATION = ("off", "whisper", "openai", "gigaam", "apple", "grok", "gemini", "zipa")   # zipa — транскрипция IPA вместо слов   # диктовка: выключена (только команды) / Whisper / OpenAI / GigaAM / Apple / Grok
KEY_COMMANDS = ("space", "double", "enter", "clear")   # команды-клавиши: в конце фразы «Apple» их жмём после текста
READING = ("off", "system", "openai", "silero", "grok", "gemini")   # озвучка ответов: выключена / голос macOS / голос OpenAI / Silero / Grok
VAD_NUMBERS = ("min_level", "start_ratio", "silence_ms", "min_speech_ms", "min_command_ms", "max_speech_s", "dictation_max_s",
               "stream_max_s", "speaking_chunk_s")   # настройки детектора речи — только числа
MODE_KEYS = ("mic", "mic_mute", "dictation", "reading", "sound",   # меняются кнопками сразу, форма настроек их не трогает
             "voice_paused", "voice_paused_at")                 # «Пауза голоса» — только действиями voice.pause / voice.resume
WORD_KEYS = ("words_ru", "words_en")
APPLE_STALE_S = 15   # итог прошлой фразы не пришёл за столько секунд — новую фразу печатаем, не дожидаясь
APPLE_MAX_FIX = 40   # печать по ходу речи: правка стирает не больше стольких characters хвоста (раньше — не трогаем)
HINT_LIMIT = 100   # подсказка распознавателю — не больше стольких слов (Apple, Grok, OpenAI)
MIC_SPEAKING = ("off", "commands", "dictation")   # (до блока «Звук») микрофон, пока говорит голос — теперь эхо: auto / on / off
# блок «Звук» (настройка sound): одна модель для окна Voice и страницы телефона
SOUND_VALUES = {"mic": ("off", "mac", "phone"),          # чей микрофон слушаем: никого / Мак / телефон
                "mic_mode": ("all", "commands", "speech"), # всё / только команды / только речь
                "speech": ("off", "mac", "phone"),       # где звучит озвучка ответов
                "echo": ("auto", "on", "off")}           # защита от эха: по устройству вывода / всегда / никогда
SOUND_DEFAULT = {"mic": "mac", "mic_mode": "all", "speech": "mac", "echo": "auto"}
PHONE_OUTPUT_DEFAULT = {"mode": "auto", "name": "headset"}   # озвучка телефона: авто (гарнитура, если есть) / вручную
ECHO_MIN_WORDS = 2   # «любая диктовка»: меньше своих слов (не из озвучки) — считаем фразу эхом
PHONE_ECHO_S = 2.0   # текст с tabletа: задержка сети сверх speech_tail_s, в которую он считается эхом озвучки
PART_KEYS = {"words_en": "parts_en"}   # два столбика -> список сочетаний   # слова-команды: отдельно для русского и английского интерфейса   # язык Whisper: как в интерфейсе (EN / RU) или автоопределение


def sound_patch(value):
    """Часть блока «Звук» (с телефона, из окна): только известные ключи с допустимыми значениями."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in SOUND_VALUES and v in SOUND_VALUES[k]}


def sound_of(cfg):
    """Блок «Звук» из настроек: все четыре ключа, неизвестное — по умолчанию."""
    return {**SOUND_DEFAULT, **sound_patch(cfg.get("sound"))}


def phone_output_of(value):
    """Озвучка телефона: {mode: auto | manual, name: speaker | headset}."""
    value = value if isinstance(value, dict) else {}
    return {"mode": "manual" if value.get("mode") == "manual" else "auto",
            "name": "speaker" if value.get("name") == "speaker" else "headset"}


def migrate_sound(cfg):
    """Старые настройки микрофона, озвучки и эха → блок «Звук» (один раз, когда sound ещё нет). True — перевели.

    Ничего не теряется: прежние ключи уходят в sound_legacy (а весь config.json — в копию config.json.bak-sound).
    phone_off — телефон ушёл из «Голоса», а Мак был выключен Voice сам: настоящий выбор — в нём, а не в mic / reading."""
    if isinstance(cfg.get("sound"), dict):
        return False
    po = cfg.get("phone_off") if isinstance(cfg.get("phone_off"), dict) else None
    on, mute, reading = cfg.get("mic", True), cfg.get("mic_mute", False), cfg.get("reading")
    sound = dict(SOUND_DEFAULT)
    if po and po.get("mic") is not None:
        # телефон был в «Голосе» и слушали его; Мак выключил Voice, когда телефон ушёл
        sound["mic"] = "phone" if (po.get("mic") or po.get("mute")) else "off"
        sound["mic_mode"] = "commands" if po.get("mute") and not po.get("mic") else "all"
        voice = po.get("reading") if po.get("reading") is not None else reading
        sound["speech"] = "off" if voice == "off" else "phone"
    else:
        sound["mic"] = "mac" if (on or mute) else "off"
        sound["mic_mode"] = "commands" if mute and not on else "all"
        voice = reading
        sound["speech"] = "off" if voice == "off" else "mac"
    # голос озвучки — отдельно от «где звучит»: «выкл» больше не голос
    voices = [v for v in ((po or {}).get("reading"), reading) if v in READING and v != "off"]
    cfg["reading"] = voices[0] if voices else "system"
    # «Микрофон, пока звучит озвучка»: выключен / только команды → защита всегда; любая диктовка → по устройству
    sound["echo"] = "auto" if cfg.get("mic_while_speaking") == "dictation" else "on"
    legacy = {k: cfg.pop(k) for k in ("mic_while_speaking", "mac_headphones", "mute_while_speaking") if k in cfg}
    legacy.update({k: cfg.get(k) for k in ("mic", "mic_mute", "phone_off", "phone_headphones") if k in cfg})
    legacy["reading"] = reading
    cfg["sound_legacy"] = legacy
    cfg["sound"] = sound
    # телефон сейчас не в «Голосе»: обрыв связи (Мак был приглушён, озвучку не трогали) или ушёл сам
    cfg["phone_off"] = ({"reason": "lost" if po.get("reading") is None and mute else "left"}
                        if po and po.get("mic") is not None else None)
    cfg["mic"], cfg["mic_mute"] = sound["mic"] != "off" and sound["mic_mode"] == "all", sound["mic_mode"] == "commands"
    cfg.setdefault("phone_output", dict(PHONE_OUTPUT_DEFAULT))
    return True


# команды, которых больше нет (2026-10-24): «дурик-мадурик» (openai) — то же, что «Диктовка OpenAI»;
# «мутабор» (standard) — вернуть Whisper. Слова «системная» / «system» перешли от Whisper к Apple («Диктовка системная»)
REMOVED_COMMANDS = ("openai", "standard", "mic_off", "dict_off")
SYSTEM_WORDS = {"parts_ru": ("системная", "систэмная", "системную", "систмная", "системной"), "parts_en": ("system",)}


def migrate_commands(cfg):
    """Набор команд 2026-10-24: убрать openai / standard (и их образцы ZIPA); «системная» — из Whisper в dict_apple.
    True — что-то поменяли. dict_apple сама приходит из config.default.json (load_config, setdefault);
    слова «системная» убираются из Whisper один раз — пока dict_apple ещё нет (потом слова — выбор владельца)."""
    cmds = cfg.get("commands")
    if not isinstance(cmds, dict):
        return False
    changed = False
    if not cfg.get("mic_commands_v2"):
        for name, verb, noun, enverb, ennoun in (
                ("mic_mute", "выключи", "микрофон", "turn off", "microphone"),
                ("mic_on", "включи", "диктовку", "turn on", "dictation")):
            cmds[name] = {"enabled": True, "parts_ru": [[verb], [noun]],
                          "words_ru": [verb + " " + noun, noun + " " + verb],
                          "parts_en": [[enverb], [ennoun]],
                          "words_en": [enverb + " " + ennoun, ennoun + " " + enverb]}
            if isinstance(cfg.get("zipa_templates"), dict):
                cfg["zipa_templates"].pop(name, None)
        cfg["mic_commands_v2"] = True
        changed = True
    for name in REMOVED_COMMANDS:
        if name in cmds:
            del cmds[name]
            changed = True
        if isinstance(cfg.get("zipa_templates"), dict) and name in cfg["zipa_templates"]:
            del cfg["zipa_templates"][name]
            changed = True
    whisper = cmds.get("dict_whisper")
    if "dict_apple" not in cmds and isinstance(whisper, dict):
        for parts_key, words in SYSTEM_WORDS.items():
            parts = whisper.get(parts_key)
            if not (isinstance(parts, list) and len(parts) > 1 and isinstance(parts[1], list)):
                continue
            rest = [w for w in parts[1] if str(w).strip().lstrip(",.").lower() not in words]
            # вторую часть целиком не опустошаем: одно «диктовка» включало бы Whisper
            if rest and len(rest) < len(parts[1]):
                parts[1] = rest
                changed = True
    return changed


def whisper_language(cfg):
    """Язык, который слушает Whisper: язык окна или auto."""
    return "auto" if cfg.get("language_mode") == "auto" else cfg.get("ui_language", "en")
# Whisper «слышит» это в тишине и шуме: приметы ищем в любом месте фразы, фразу целиком не печатаем
JUNK_MARKERS = ("субтитр", "семкин", "егорова", "корректор а ", "dimatorzok", "amara org",
                "продолжение следует", "thanks for watching", "thank you for watching")
JUNK_EXACT = ("you", "thank you", "thank you so much", "bye", "bye bye")   # фраза только из этого — шум
VOWELS = set("аеёиоуыэюяйaeiouy")     # фраза из одних гласных («а», «э», «ой») — вздох, а не речь
ROUTES = ("orca", "keys", "applescript", "smart")   # куда уходят клавиши и текст: Orca, терминал напрямую
                                                    # или smart — по приложению в фокусе в момент команды


_MISSING = object()


def config_diff(old, new, prefix=""):
    """Что изменилось в настройках: [(путь, было, стало)], вложенные словари — по ключам."""
    out = []
    for key in sorted(set(old) | set(new), key=str):
        a, b = old.get(key, _MISSING), new.get(key, _MISSING)
        path = f"{prefix}{key}"
        if isinstance(a, dict) and isinstance(b, dict):
            out += config_diff(a, b, path + ".")
        elif a != b:
            out.append((path, a, b))
    return out


def short_value(value, limit=200):
    """Значение настройки для журнала — одной строкой, длинное обрезаем."""
    if value is _MISSING:
        return "—"
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


class NewestFirstHandler(logging.Handler):
    """Журнал в файл, свежие записи сверху. Файл обрезается до max_lines строк.

    archive — папка: с новым днём записи прошлых дней уходят туда, по файлу на день.
    """

    STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def __init__(self, path, archive=None, max_lines=3000):
        super().__init__()
        self.path = Path(path)
        self.archive = Path(archive) if archive else None
        self.max_lines = max_lines
        self._day = None
        self._flip_old_file()
        self._archive_old_days()

    def _read(self):
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _write(self, lines):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[:self.max_lines]) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)     # целиком, чтобы просмотрщик не увидел пустой файл

    def _flip_old_file(self):
        """Старый журнал (свежие внизу) один раз переворачиваем, многострочные записи не разрываем."""
        lines = self._read()
        stamps = [line[:23] for line in lines if self.STAMP.match(line)]
        if len(stamps) < 2 or stamps[0] >= stamps[-1]:
            return
        records = self._records(lines)
        self._write([line for rec in reversed(records) for line in rec])

    def _records(self, lines):
        """Строки -> записи (многострочная запись — вместе со своей первой строкой)."""
        records = []
        for line in lines:
            if self.STAMP.match(line) or not records:
                records.append([line])
            else:
                records[-1].append(line)
        return records

    def _archive_old_days(self):
        """Записи прошлых дней — в archive/<журнал>-ГГГГ-ММ-ДД.log (свежие сверху); в журнале остаётся сегодня."""
        today = time.strftime("%Y-%m-%d")
        self._day = today
        if not self.archive:
            return
        keep, old = [], {}
        for rec in self._records(self._read()):
            day = rec[0][:10] if self.STAMP.match(rec[0]) else today
            if day == today:
                keep.extend(rec)
            else:
                old.setdefault(day, []).extend(rec)
        if not old:
            return
        self.archive.mkdir(parents=True, exist_ok=True)
        for day, lines in old.items():
            dest = self.archive / f"{self.path.stem}-{day}{self.path.suffix}"
            try:
                before = dest.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                before = []
            tmp = dest.with_suffix(".tmp")
            tmp.write_text("\n".join(lines + before) + "\n", encoding="utf-8")   # из журнала — новее архивных
            os.replace(tmp, dest)
            try:
                page = log_html.render(lines + before, f"GOBL(in) Voice — {day}")
                dest.with_suffix(".html").write_text(page, encoding="utf-8")
            except Exception:
                pass        # HTML — для удобства, архив .log уже записан
        self._write(keep)

    def emit(self, record):
        try:
            if time.strftime("%Y-%m-%d") != self._day:
                self._archive_old_days()       # наступил новый день — вчерашнее в архив
            self._write(self.format(record).splitlines() + self._read())
        except Exception:
            self.handleError(record)


class DailyFileHandler(logging.FileHandler):
    """Подробный журнал: всё подряд по порядку (свежие внизу), без лимита строк.
    С новым днём (и при запуске) файл прошлого дня уходит в archive/<журнал>-ГГГГ-ММ-ДД.log."""

    def __init__(self, path, archive):
        self.archive = Path(archive)
        self._day = time.strftime("%Y-%m-%d")
        self._roll(Path(path))
        super().__init__(path, encoding="utf-8")

    def _roll(self, path):
        """Файл не за сегодня — в архив (за тот же день там уже есть — дописываем в конец)."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                first = f.readline()
        except FileNotFoundError:
            return
        day = first[:10] if NewestFirstHandler.STAMP.match(first) else time.strftime(
            "%Y-%m-%d", time.localtime(path.stat().st_mtime))
        if day == self._day:
            return
        self.archive.mkdir(parents=True, exist_ok=True)
        dest = self.archive / f"{path.stem}-{day}{path.suffix}"
        if dest.exists():
            with open(dest, "a", encoding="utf-8") as out:
                out.write(path.read_text(encoding="utf-8", errors="replace"))
            path.unlink()
        else:
            os.replace(path, dest)

    def emit(self, record):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self.close()                      # FileHandler сам откроет файл заново при записи
            self._day = today
            self._roll(Path(self.baseFilename))
        super().emit(record)


class QuietLibs(logging.Filter):
    """Обычный журнал: от шумных библиотек — только предупреждения и ошибки."""

    def filter(self, record):
        return record.levelno >= logging.WARNING or not record.name.startswith(NOISY_LOGGERS)


FULL_HANDLER = None     # подробный журнал: отладочные записи (DEBUG) пишет, только пока включена «Отладка»


def set_debug_log(on):
    """Настройка debug_log: вкл — в подробный журнал идёт всё (DEBUG), выкл — только INFO и выше."""
    if FULL_HANDLER:
        FULL_HANDLER.setLevel(logging.DEBUG if on else logging.INFO)


def setup_log():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    full_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s · %(threadName)s] %(message)s")

    short = NewestFirstHandler(LOG_FILE, LOG_ARCHIVE)     # обычный журнал (пульт): INFO и выше
    console = logging.StreamHandler()
    for h in (short, console):
        h.setLevel(logging.INFO)
        h.addFilter(QuietLibs())
        h.setFormatter(fmt)
    global FULL_HANDLER
    full = FULL_HANDLER = DailyFileHandler(FULL_LOG, LOG_ARCHIVE)   # подробный: всё, что пишется
    full.setLevel(logging.DEBUG)                        # до чтения настроек; дальше — set_debug_log
    full.setFormatter(full_fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (short, console, full):
        root.addHandler(h)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.INFO)  # их INFO — только в подробный журнал
    log = logging.getLogger("orca-voice")
    log.setLevel(logging.DEBUG)                         # наши подробности (debug) — только в подробный

    # необработанные ошибки в потоках — в журнал, а не только в stdout
    def thread_crash(args):
        log.error("Thread %s failed", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_crash
    return log


# ---------- прошлые запуски

def _pid_cwd(pid):
    """Рабочая папка процесса (через lsof)."""
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True).stdout
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), "")


def _our_processes():
    """pid прошлых запусков: строго процесс Python, у которого скрипт — наш app.py."""
    me = os.getpid()
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    found = set()
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        parts = command.split()
        if len(parts) < 2 or not Path(parts[0]).name.lower().startswith("python"):
            continue    # не Python (например, оболочка, где просто упомянут путь)
        pid = int(pid_text)
        script = parts[1]
        if pid != me and (script == str(HERE / "app.py")
                          or (script == "app.py" and _pid_cwd(pid) == str(HERE))
                          or (script == "core/app.py" and _pid_cwd(pid) == str(ROOT))):   # start.sh / .app: cd utility/
            found.add(pid)
    return found


def kill_previous(log):
    """Убивает прошлые запуски GOBL(in) Voice (их whisper-server гасится при старте нового)."""
    victims = _our_processes()
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
            log.info(t("log.killed_previous"), pid)
        except ProcessLookupError:
            pass

    # кто не ушёл за секунду — добиваем (проверяем заново, что это всё ещё наш процесс)
    if victims:
        time.sleep(1.0)
        for pid in victims & _our_processes():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # ждём, пока процессы реально исчезнут (до 5 с)
        deadline = time.time() + 5
        while victims & _our_processes() and time.time() < deadline:
            time.sleep(0.2)

    PID_FILE.write_text(str(os.getpid()))


# ---------- ядро

class OrcaVoice:
    def __init__(self, cfg, log):
        self._preview_lock = threading.RLock()
        self.cfg = cfg
        audio_out.configure(cfg)                       # звуковые устройства: как в macOS или выбранные вручную
        self.log = log

        # общее состояние (его читает и AnswerWatcher)
        # live — сейчас распознаёт OpenAI; speak_answers — озвучка включена
        self.state = {"speak_answers": sound_of(cfg)["speech"] != "off", "last_target": None, "live": False}
        self.status = t("st.whisper_start")
        self.heard = "—"
        self.ready = False
        self.error = None

        # части приложения
        self.whisper = WhisperServer(cfg, log)
        self.whisper_lock = threading.Lock()
        self._backup_pool = None                      # поток страховки ZIPA (см. _backup_start)
        self._backup_future = None
        self._backup_epoch = 0
        self._backup_clients = {
            "apple": AppleSTT(cfg, log), "grok": GrokSTT(cfg, log),
            "openai": OpenAISTT(cfg, log), "gemini": GeminiSTT(log),
            "subscription": SubscriptionSTT(cfg, log),
        }
        self._subscription_stt = SubscriptionSTT(cfg, log)
        self.kw_awake = False                         # режим «по ключевому слову»: диктовка разбужена
        self.kw_at = 0                                # когда разбудили (или печатали в последний раз)
        self.KW_SLEEP_S = 30                          # столько тишины — и снова ждём слово-ключ
        self.ui_seen = 0                              # когда окно последний раз отчиталось (ui_log)
        self.matcher = Matcher(cfg)
        self.speaker = Speaker(cfg["voice"], cfg["rate"], log)
        self.speaker.remote = self._remote_voice      # озвучка голосом OpenAI, если выбрана
        self.gigaam = GigaAM(cfg, log)                # локальное распознавание (диктовка «GigaAM»)
        self.zipa = zipa_ipa.Zipa(cfg, log)           # диктовка «ZIPA»: фонемы IPA вместо слов (свой процесс с моделью)
        self.silero = Silero(cfg, log)                # локальный русский голос (озвучка «Silero»)
        self.ear = fuzzy.FuzzyEar(self.gigaam)        # «нечёткий слух»: Enter в конце фразы по звуку
        # диктовка «Apple»: распознаватель macOS потоком — печатаем по ходу речи и правим, как штатная диктовка
        self.apple = AppleSTT(cfg, log)
        self.apple.on_partial = lambda sid, text: self.apple_q.put((sid, text))
        self.apple_q = queue.Queue()                  # текст по ходу речи -> поток печати
        self.apple_lock = threading.Lock()            # печать по ходу речи и окончательный текст — по очереди
        self.apple_st = None                          # фраза, которую сейчас печатаем: id, напечатано, вкладка
        self.apple_order = []                         # незаконченные фразы по порядку начала: [id, когда была активна]
        self.apple_type_lock = threading.Lock()       # клавиши правки — одна правка за раз (иначе нажатия перемешаются)
        self.apple_done = set()                       # фразы, по которым уже пришёл окончательный текст
        self._apple_sync = threading.Lock()
        # диктовка и озвучка «Grok» (xAI): печать по ходу речи — тем же путём, что «Apple»
        self.grok = GrokSTT(cfg, log)
        self.gemini = GeminiSTT(log)
        self.gemini.on_partial = lambda sid, text: self.apple_q.put((sid, text))
        self.grok.on_partial = lambda sid, text: self.apple_q.put((sid, text))
        self.grok_tts = GrokTTS(cfg, log)
        self.gemini_tts = GeminiTTS(cfg, log)
        self.gemini_tts.on_error = self._gemini_voice_error
        self.gemini_tts.on_prepare = lambda: self.play("gemini_prepare", 0.3)
        # OpenAI по ключу API (openai_auth = api): то же, что Grok; по подписке — live.py (вход Codex)
        self.oa = OpenAISTT(cfg, log)
        self.oa.on_partial = lambda sid, text: self.apple_q.put((sid, text))
        self.oa_tts = OpenAITTS(cfg, log)
        self.speaker.local = self.silero
        self.speaker.grok = self.grok_tts
        self.speaker.cloud = self._cloud_voice     # Grok или OpenAI по ключу — озвучка через HTTP
        self.speaker.phone_audio = self._phone_audio
        self.speaker.phone_audio_stop = self._phone_audio_stop
        self._phone_token = 0                         # новая фраза или «заткнись» обрывают отправку прежней
        self.speaker.engine = self._speaker_engine
        self.speaker.selection_for = self._answer_selection
        self.speaker.voices_for = self._answer_voices
        self.speaker.engine_for = self._answer_engine
        self.speaker.rate_for = self._answer_rate
        self.speaker.muted = self.paused               # «Пауза голоса»: ничего не читаем (ни на Маке, ни на телефоне)
        # tablet через Goblin Remote: пока он в режиме «Голос», микрофон и звук Мака выключены
        self.phone = Remote(log)
        self.release_pending = False     # «держи и говори»: отпустили кнопку — после фразы нажать Enter
        self.typed_pending = False       # что-то напечатано и ещё не отправлено Enter'ом
        self.phone.on_change = self._phone_changed
        self.phone.on_mic = self._phone_mic
        self.phone.on_reply = self._phone_reply
        self.phone.echo = self.echo_now
        self.speaker.phone = self.phone
        self.watcher = AnswerWatcher(cfg, self.speaker, log, self.state)
        self.watcher.on_answer = lambda ev: self.phone.answer(ev.get("agent", ""), ev.get("message", ""))
        from work_timers import WorkTimers
        self.work_timers = WorkTimers()
        self._work_shutdown = threading.Event()
        self._work_focus = {}
        self._work_enabled = bool(cfg.get("work_tick", True))
        self.watcher.on_done = self._work_answer        # агент ответил (не промежуточно) — «агент работает» молчит
        self.watcher.front = self.answer_front          # озвучка «только активное окно»: что сейчас впереди
        # «Оркестрация»: диалог агентов в разных вкладках Terminal через Voice («трансфер кодекс 2», «трансфер боря»)
        self.orch = orchestration.Orchestra(cfg, log, LOGS / "orchestration.jsonl")
        from terminal_voice_profiles import TerminalVoiceProfiles
        self.terminal_voices = TerminalVoiceProfiles(LOGS.parent / "data" / "state" / "terminal-voices.json")
        from terminal_sources import TerminalSources
        self.terminal_sources = TerminalSources(registry=self.watcher.hooks.registry)
        from codex_terminal_stream import CodexTerminalStream
        self.codex_terminal_stream = CodexTerminalStream(log)
        self.watcher.route_codex = lambda events: self.codex_terminal_stream.filter_logs(
            events, include_interim=self.cfg.get("answer_signal", "log") == "hooks"
            and bool(self.cfg.get("codex_interim", False)))
        self.watcher.fast_source = lambda: self.codex_terminal_stream.route([], self.orch.speech_sessions())
        self.watcher.resolve_source = lambda ev: self.terminal_sources.enrich(ev, self.orch.speech_sessions())
        self.watcher.hooks.sessions = self.orch.speech_sessions
        self.watcher.skip = lambda ev: False  # Межагентный чат озвучивается по общим настройкам.
        self.reader = html_reader.Reader(self.speaker, log)   # вкладка «Чтение»: HTML-файл вслух
        self.recorder = voice_tests.Recorder(log)     # вкладка «Тесты»: запись вашего голоса для базы
        self.synth = voice_tests.Synth(self, log)     # вкладка «Тесты»: те же тексты голосами озвучки
        # чтение файла: с «Очередью» ответы ждут его конца, без неё — перебивают, и чтение потом продолжается
        self.watcher.talking = lambda: self.user_talking() or (self.reader.active and self.watcher._queue())
        self.watcher.before_say = self.reader.interrupt
        self.handling = False                           # фраза сейчас распознаётся / выполняется
        self._signal = threading.local()                # последний сигнал, сыгранный при разборе фразы (свой у потока)
        self._quiet_signals = []                        # процессы сигнала «фраза напечатана»: микрофон из-за них не глохнет

        self.settings = SettingsServer(self, cfg.get("settings_port", 8792), log)
        self.direct = direct.Direct(log)     # терминалы без Orca
        self.last_term = LastTerminal(self.direct, log)   # «Активный терминал»: куда вернуться
        self.jonny = Jonny(self.direct, log)     # свой терминал агента: «Джони, …» — фраза туда до Enter
        self.jonny.cfg = cfg.get("jonny") or {}
        from input_focus import InputFocus
        self.input_focus = InputFocus(self.direct, log)
        # Your phone is disconnected from Voice: lost — обрыв связи (Мак слушает команды, озвучка на Маке), left — ушёл сам
        # (микрофон и озвучка «телефон» молчат); хранится в config.json (phone_off) — переживает перезапуск
        po = cfg.get("phone_off") if isinstance(cfg.get("phone_off"), dict) else {}
        self._phone_gone = "lost" if po.get("reason") == "lost" else "left"
        self.listener = None
        self._apply_sound()
        self.phrases = queue.Queue()

        # live-режим
        self.live = None
        self.live_started = 0.0
        self.live_lock = threading.Lock()
        self.live_retry_at = 0.0    # после обрыва: когда переподключаться
        self.live_fails = 0
        self.ignore_until = 0.0     # короткая глухота после выхода из live

    # ---------- запуск / выход

    def start(self):
        self.settings.start()   # окно грузится с этого сервера — поднимаем сразу
        from media_controllers import ControllerManager
        self.media_keys = ControllerManager(self)
        self.media_keys.start()
        self.orch.start()       # «Оркестрация» не зависит от микрофона и моделей — сразу
        threading.Thread(target=self.boot, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.ticker, daemon=True).start()
        threading.Thread(target=self._work_loop, daemon=True, name="work-clock").start()
        threading.Thread(target=self._work_observer, daemon=True, name="work-observer").start()
        threading.Thread(target=self._apple_typer, daemon=True).start()

    def boot(self):
        whisper_failed = None
        try:
            self.whisper.start()
        except Exception as e:
            # без Whisper не работают только команды и диктовка Whisper; Apple / Grok / OpenAI / телефон — работают.
            # Voice поднимаем дальше, а Whisper сам попробует запуститься снова при первой фразе для него
            whisper_failed = e
            self.status = t("st.error", e)
            self.log.exception(t("log.boot_failed"))
        try:
            self._preload_local()
            self.listener = Listener(self.cfg["vad"], self.on_phrase, self.log)
            self.listener.audio_diagnostics.enabled = bool(self.cfg.get("audio_diagnostics", False))
            self.listener.input_name = audio_out.chosen("in")
            self.listener.on_input_device = self._mic_input_device
            # пока говорит голос (и ещё speech_tail_s после) — не печатаем, а при mute — не слушаем вовсе
            self.listener.hold = lambda: self.speaker.busy_or_recent(self.cfg.get("speech_tail_s", 1.0))
            self.listener.dictation = self.dictation() != "off"
            self.listener.on_drop = self._apple_drop
            self.listener.on_probe = self._wake_probe   # «дружище» на лету — колокольчик, пока ещё говорят
            self.listener.on_start = self._apple_start
            self.listener.on_missing = self._mic_missing
            self.listener.echo_free = self._echo_free
            self.listener.short_sound_stop = self._short_sound_stop
            self.listener.on_barge = self._barge
            self.listener.self_playing = self.speaker.busy
            self.listener.quiet_pids = self._quiet_pids
            self.phone.feed = self.listener.feed_remote
            # микрофон, эхо, чужой звук — по блоку «Звук»; tablet мог включить «Голос» раньше, чем появился
            # детектор речи, — тогда слушаем его сразу
            self._apply_sound()
            self.listener.start()
            self._sync_apple()
            self.watcher.start()
            # Ведущий назначается владельцем вручную; при старте терминал не открываем.
            self.ready = True
            if not whisper_failed and not self.listener.missing:   # предупреждение о микрофоне не затираем
                self.status = t("st.standard")
            self.sync_live()            # диктовка или голос OpenAI — подключаемся сразу
        except Exception as e:
            self.error = str(e)
            self.status = t("st.error", e)
            self.log.exception(t("log.boot_failed"))

    def shutdown(self):
        self._work_shutdown.set()
        self._stop_backup()
        self._subscription_stt.stop()
        self.last_term.checkpoint()
        self.log.info(t("log.exit"))
        with self.live_lock:
            if self.live:
                self.live.stop()
                self.live = None
        if self.listener:
            self.listener.stop()
        self.gemini_tts.close()
        self.gemini.stop()
        self.apple.stop()
        self.grok.stop()
        self.oa.stop()
        self.watcher.stop()
        if getattr(self, "media_keys", None):
            self.media_keys.stop()
        self.orch.stop()
        self.speaker.stop()
        self.whisper.stop()
        self.zipa.stop()
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass

    def ticker(self):
        """Раз в секунду: автовозврат диктовки OpenAI к Whisper, переподключение OpenAI,
        запоминание терминала в фокусе."""
        while True:
            try:
                self._tick()
            except Exception:
                # ticker живёт до выхода: без него нет переподключения OpenAI и возврата звука от телефона
                self.log.exception("Ticker failed and will continue")
            time.sleep(1)

    def _tick(self):
        limit = self.cfg.get("live_max_minutes", 10) * 60          # 0 — «никогда»
        if limit and self.state["live"] and time.time() - self.live_started > limit:
            try:
                self.set_dictation("whisper", "timeout")
            except Exception as e:
                self.log.warning("live: %s", e)
        if self.ready:
            try:
                self.sync_live()
            except Exception as e:
                self.log.warning("live: %s", e)
        try:
            self.phone.tick()
        except Exception as e:
            self.log.warning("tablet: %s", e)
        try:
            self._apply_sound()                  # наушники подключили / отключили — эхо «авто» меняется само
        except Exception as e:
            self.log.warning("audio: %s", e)
        try:
            self.last_term.tick()
        except Exception as e:
            self.log.warning("active terminal: %s", e)
        if self.route() != "orca":
            try:
                self.direct.remember_front()
            except Exception:
                pass

    def _phone_changed(self, active, reason="bridge"):
        """Планшет включил или выключил режим «Голос». reason: bridge — так сообщил мост, timeout — мост пропал
        (обрыв связи), taken — микрофон забрал Мак.

        Настройки блока «Звук» при этом не меняются — меняется только то, что из них сейчас работает:
        - вернулся: микрофон «телефон» слушает его, озвучка «телефон» звучит на нём;
        - ушёл сам (мост сообщил): микрофон и озвучка «телефон» молчат — Мак не подхватит разговор;
        - обрыв связи: Мак слушает только команды («Амиго» / «Джони» / «включи диктовку»), озвучка — на Маке.
        Микрофон и озвучка «Мак» от телефона не зависят вовсе."""
        sound = self.sound()
        if active:
            self._phone_gone = None
            self._phone_off_keep()
            # Первое сердцебиение сообщает состояние устройства. Дальнейшие
            # изменения кнопок старой страницы обрабатывает Remote.set_state.
            if self.sound()["speech"] == "phone":
                self.speaker.stop()              # то, что звучало на Маке, — замолкает
            if self.sound()["speech"] != "off":   # страница узнаёт, где звучат ответы, — по выбору Voice
                self.phone.set_reply("mac" if self.sound()["speech"] == "mac" else "text")
            self.log.info("phone in Voice mode — microphone: %s, speech: %s", self.sound()["mic"], self.sound()["speech"])
        else:
            self._phone_gone = "lost" if reason == "timeout" else "left"
            self._phone_off_keep()
            if self._phone_gone == "lost" and "phone" in (sound["mic"], sound["speech"]):
                self.log.info("phone disconnected — the Mac hears commands only and plays speech locally")
            elif "phone" in (sound["mic"], sound["speech"]):
                self.log.info("phone left Voice mode — its microphone and speech are paused until it returns")
            else:
                self.log.info("phone left Voice mode — microphone and speech remain on the Mac")
        self._apply_sound()

    _phone_gone = "left"     # (до __init__ — для тестов без запуска)

    def _phone_off_keep(self):
        """Почему телефона нет (lost / left) — в config.json: перезапуск Voice без телефона ведёт себя так же."""
        value = {"reason": self._phone_gone} if self._phone_gone else None
        if getattr(self, "cfg", None) is not None and self.cfg.get("phone_off") != value:
            self.save_setting("phone_off", value)

    def _mic_missing(self, wanted, fallback):
        """Выбранный вручную микрофон пропал (Listener): не молча — статус в окне, сигнал «ошибка», строка на телефон."""
        en = self.cfg.get("ui_language") == "en"
        if fallback:
            self.status = ((f"⚠ “{wanted}” is not connected — listening to “{fallback}”"))
        else:
            self.status = ((f"⚠ “{wanted}” is not connected — not listening"))
        self.play("error")
        self.phone.command(self.status)

    def _echo_free(self):
        """Эха озвучки в микрофоне нет (блок «Звук» → Эхо): речь поверх голоса — ввод, фильтр своих слов не нужен."""
        return not self.echo_now()

    def _barge(self):
        """Заговорили поверх голоса в наушниках («любая диктовка»): голос и чтение замолкают сразу."""
        active = self.speaker.busy()
        self.reader.stop()
        self.speaker.stop()
        self.log.info("Speech stopped because you spoke over it on headphones")
        return active

    def _barge_stop_word(self, text, plain=False):
        """Отдельные «так» / «хорошо»: только для уже подтверждённого прерывания озвучки."""
        if self.dictation() == "zipa" and not plain:
            phones = (text or "").split()
            while phones and zipa_ipa.pauses.is_pause(phones[0]):
                phones.pop(0)
            while phones and zipa_ipa.pauses.is_pause(phones[-1]):
                phones.pop()
            if any(zipa_ipa.pauses.is_pause(p) for p in phones):
                return False
            value = "".join(zipa_ipa._units(phones))
        else:
            words = re.findall(r"\w+", (text or "").lower())
            if len(words) != 1:
                return False
            value = words[0]
            if value == "tak":
                return True
            value = value.replace("kh", "х").replace("sh", "ш")
            value = value.translate(str.maketrans({"h": "х", "x": "х", "r": "р",
                                                   "a": "а", "o": "о", "u": "у"}))
        # «харашо», «харошо», «хорашо», «хорошоо» и фонетическое «хорошоу» — то же слово.
        return value == "так" or re.fullmatch(r"х[аоу]р[аоу]ш(?:о{1,3}|а{1,3}|у{1,3}|оу)", value) is not None

    def _barge_control(self, text, interrupted=False, sid=None, plain=False, backup=None):
        if not interrupted or self.speech_only():
            return False
        matched = self._barge_stop_word(text, plain)
        if not matched and backup is not None:
            try:
                said = self._backup_result(backup)
            except Exception:
                said = ""
            matched = self._barge_stop_word(said, plain=True)
            if matched:
                self.log.info("Speech interrupted after fallback recognition: %r; ZIPA: %r", said, text)
        if not matched:
            return False
        if sid:
            self._apple_final(sid, "", junk=True)
        self.fire(["stop"])
        self.log.info("Speech interrupted by an acknowledgment")
        return True

    def _phone_mic(self, mic):
        """Кнопка микрофона на странице (старое приложение 1.2.1: tablet / mac / off) — это группа «Микрофон»."""
        self.set_sound({"mic": {"tablet": "phone", "mac": "mac", "off": "off"}[mic]}, source="phone")

    def _phone_reply(self, reply):
        """Где звучат ответы — выбор на странице (text — телефон, mac — Мак): это группа «Озвучка»."""
        self.set_sound({"speech": "mac" if reply == "mac" else "phone"}, source="phone")

    def phone_release(self):
        """«Hold to talk»: кнопку отпустили — договариваем фразу и отправляем её (Enter).
        В поток звука — секунда тишины, чтобы фраза сразу закончилась; Enter нажмёт handle_speech
        после печати, а если новой фразы нет — через RELEASE_WAIT, если что-то напечатано и не отправлено."""
        self.log.info("Tablet button released: send the phrase")
        with self.input_lock:
            self.release_pending = True
            epoch = self.input_epoch
        self.phone.audio(b"\x00\x00" * 16000)
        threading.Thread(target=self._enter_after_release, args=(epoch,), daemon=True).start()

    def _enter_after_release(self, epoch=None):
        if epoch is None:
            epoch = self.input_epoch
        deadline = time.time() + RELEASE_WAIT
        while time.time() < deadline:
            if not self.release_pending or epoch != self.input_epoch:
                return                          # Enter уже нажат после печати фразы
            time.sleep(0.1)
        with self.input_lock:
            if not self.release_pending or epoch != self.input_epoch:
                return
            self.release_pending = False
            if self.typed_pending:
                term = self._focused()
                if term:
                    self._send_key(term, "enter")

    def phone_text(self, text):
        """Готовый текст с tabletа (распознан там): как будто его надиктовали — печать и команды."""
        text = (text or "").strip()
        if not text:
            return
        if self.paused():
            self.log.debug("Tablet text skipped while Voice is paused: %s", text)
            return
        # «Звук на Маке»: микрофон tabletа слышит озвучку Мака, а текст распознан на tabletе и глушению звука
        # не подлежит — текст, пришедший пока голос звучал или вскоре после (с учётом задержки сети), — эхо
        # эхо возможно только из колонок Мака («Звук на Маке»); tablet speech он сам не распознаёт
        if self._speech_where() == "mac" and self.echo_now() \
                and self.speaker.quiet_for() < self.cfg.get("speech_tail_s", 1.0) + PHONE_ECHO_S:
            if "stop" in self._commands(text, long=True):   # готовый текст: пауз перед словами не знаем
                self.fire(["stop"])               # «заткнись» поверх голоса — работает
                return
            self.log.info("Tablet speech echo skipped: %s", text)
            return
        interrupted = self.speaker.busy() and self._echo_free()
        if interrupted:
            if self._barge_control(text, True, plain=True):
                return
            self._barge()
        self.heard = text
        self.log.info("Tablet: %s", text)
        threading.Thread(target=self._phone_speech, args=(text,), daemon=True).start()

    _phone_lock = threading.Lock()

    def _phone_speech(self, text):
        """Фразы с tabletа печатаются строго по очереди (каждая в своём потоке — могли обогнать друг друга)."""
        with self._phone_lock:
            if self._mic_paused():
                return                           # микрофон выключен полностью — не слышим и телефон
            if self.mic_muted():
                self._wake(text, plain=True)     # приглушён: только «Амиго» / «Джони» / «включи диктовку»
                return
            self.handle_speech(text, words=True)   # телефон присылает слова, а не звуки ZIPA

    # что можно менять с tabletа (остальное — только в окне на Маке)
    PHONE_ACTIONS = ("dictation.off", "dictation.whisper", "dictation.openai", "dictation.gigaam", "dictation.grok", "dictation.gemini", "dictation.apple", "dictation.zipa", "reading.off", "reading.system",
                     "reading.openai", "reading.silero", "reading.grok", "reading.gemini", "hush", "space", "double", "enter", "recognize_send", "clear", "switch", "previous", "project", "active", "release", "test",
                     "mic.on", "mic.off", "mic.mute", "voice.pause", "voice.resume", "buttons.signal.terminal", "buttons.signal.standard", "buttons.signal.extra1", "buttons.signal.extra2", "tab1", "tab2", "tab3", "tab4", "tab5")
    # phone_headphones — факт с телефона (гарнитура подключена), а не выбор: по нему работает эхо «авто»
    PHONE_CONFIG = {"voice": str, "rate": int, "sounds": bool, "target": str, "phone_sounds": dict,
                    "keyword_mode": bool,
                    "phone_headphones": bool, "phone_input": dict, "phone_gain": dict,
                    "sound": dict, "phone_output": dict, "media_controls": dict, "audio_out": dict, "audio_in": dict}

    def phone_control(self, item):
        """Команда или настройка с tabletа: {kind: action|config|lang, ...}."""
        self.log.debug("tablet: %s", short_value(item))
        kind = item.get("kind")
        if kind == "action" and item.get("name") == "release":
            self.phone_release()
        elif kind == "action" and item.get("name") == "test":
            self.log.info("Tablet audio check")
            self.speaker.test()                  # «Звук на Маке» — фраза звучит здесь, иначе — на tabletе
        elif kind == "action" and item.get("name") in self.PHONE_ACTIONS:
            self.log.info("Tablet: %s", item["name"])
            self.action(item["name"], source="phone")
        elif kind == "config" and (isinstance(item.get("values"), dict) or "key" in item):
            # {"values": {ключ: значение}}; запасной вид — {"key": ключ, "value": значение}
            raw = item["values"] if isinstance(item.get("values"), dict) else {str(item.get("key")): item.get("value")}
            values = {k: v for k, v in raw.items()
                      if k in self.PHONE_CONFIG and isinstance(v, self.PHONE_CONFIG[k])}
            media_dictation = (media_controls.settings(self.cfg)["enabled"]
                               and (values.get("sound") or {}).get("mic_mode") == "all")
            if media_dictation:
                self.kw_awake, self.kw_at = True, time.time()
            if "media_controls" in values:
                controls = values["media_controls"]
                if set(controls) - {"source", "enabled", "mac_device", "remote_devices"}:
                    raise ValueError("Invalid button settings")
                if "enabled" in controls and not isinstance(controls["enabled"], bool):
                    raise ValueError("Invalid button state")
                values["media_controls"] = {**media_controls.settings(self.cfg), **controls}
            if "sound" in values or "phone_output" in values:
                self.phone.knows_sound = True    # страница 1.3.0+: её voice_state.mic / reply — факт, не настройка
            if "sound" in values:                # блок «Звук»: часть из mic | mic_mode | speech | echo
                patch = sound_patch(values.pop("sound"))
                if patch:
                    media_start = media_dictation and patch == {"mic": "phone", "mic_mode": "all"}
                    if media_start and self.speech_only():
                        patch["mic_mode"] = "speech"
                    self.log.info("Tablet sound: %s", patch)
                    self.set_sound(patch, source="phone")
                    if media_start:
                        self.play("mic_on")
                else:
                    self.log.warning("Tablet sound has no valid settings: %s", short_value(raw.get("sound")))
            if "rate" in values:
                values["rate"] = max(100, min(400, values["rate"]))
            if "phone_gain" in values:           # усиление речи телефона: не больше ×max, речь громче шума в ratio раз
                pg = values["phone_gain"]
                try:
                    values["phone_gain"] = {"max": max(1, min(12, float(pg.get("max", 12)))),
                                            "ratio": max(2, min(8, float(pg.get("ratio", 4))))}
                except (TypeError, ValueError):
                    values.pop("phone_gain")
            for key, direction in (("audio_out", "out"), ("audio_in", "in")):
                if key not in values:
                    continue
                device = values[key]
                if (device.get("mode") not in ("auto", "manual") or
                        not isinstance(device.get("name", ""), str) or
                        (device.get("mode") == "manual" and device.get("name") not in audio_out.devices()[direction])):
                    raise ValueError("Mac audio device unavailable")
                values[key] = {**(self.cfg.get(key) or {}), "mode": device["mode"], "name": device.get("name", "")}
            if "phone_output" in values:         # озвучка телефона: auto | manual + speaker | headset
                values["phone_output"] = phone_output_of(values["phone_output"])
            if "phone_input" in values:          # микрофон телефона: auto | manual + phone | headset
                pm = values["phone_input"]
                values["phone_input"] = {"mode": "manual" if pm.get("mode") == "manual" else "auto",
                                       "name": "phone" if pm.get("name") == "phone" else "headset"}
            if "phone_sounds" in values:         # только известные переключатели, да/нет
                values["phone_sounds"] = {**(self.cfg.get("phone_sounds") or {}),
                                          **{k: bool(v) for k, v in values["phone_sounds"].items()
                                             if k in ("sent", "text", "enter", "clear", "beep")}}
            if values:
                self.log.info("Tablet settings: %s", values)
                self.apply_config(values)
        elif kind == "lang" and item.get("lang") == "en":
            self.set_language(item["lang"])
        else:
            raise ValueError("unknown phone command")

    def _phone_audio(self, text):
        """Озвучка на телефон звуком: выбран Silero или Grok, а телефон в «Streamingе» (в «Тексте» — его голос).
        Синтез по предложениям: первый кусок уходит, пока готовятся следующие. False — читает голос телефона."""
        engine = self._speaker_engine()
        if self.phone.mode == "text" or engine not in ("silero", "grok", "openai", "gemini"):
            return False                          # «Системный» и режим «Текст» — голос самого телефона
        if engine == "openai" and not self.openai_api():
            return self._phone_openai(text)
        if engine == "silero" and not self.silero.suits(text):
            return False                          # Silero — только русский
        self._phone_token += 1
        token = self._phone_token
        self.phone.until = time.time() + 2       # пока синтезируется — уже «говорит» (эхо, очередь ответов)
        own = dict(self.speaker.selected_voices)   # Capture this utterance's profile before the worker starts.

        def run():
            sent = 0
            try:
                if engine == "silero":
                    for audio, rate in self.silero.render(text, voice=own.get("silero"), rate=self.speaker.playback_rate):
                        if token != self._phone_token:
                            return
                        self.phone.speak_audio(self._to_m4a(audio, rate), "audio/mp4", len(audio) / rate)
                        sent += 1
                else:
                    tts, bps = (self.oa_tts, 16000) if engine == "openai" else (self.grok_tts, 8000)   # байт в секунду MP3
                    if engine == "gemini": tts, bps = self.gemini_tts, 8000
                    voice = own.get(engine)
                    for mp3 in tts.render(text, voice=voice, rate=self.speaker.playback_rate):
                        if token != self._phone_token:
                            return
                        self.phone.speak_audio(mp3, "audio/mpeg", len(mp3) / bps)
                        sent += 1
                self.log.debug("Spoke on phone with %s, %d chunks, %d characters", engine, sent, len(text))
            except Exception as e:
                self.log.warning("Phone speech (%s): %s", engine, e)
                if engine == "gemini":
                    self._gemini_voice_error(str(e))
                    return
                if not sent and token == self._phone_token:
                    self.phone.speak(text, self.cfg["voice"], self.speaker.playback_rate)
        threading.Thread(target=run, daemon=True).start()
        return True

    def _openai_auth_info(self):
        """Для окна: OpenAI — подписка (вход Codex) или ключ API."""
        if self.cfg.get("openai_auth", "subscription") == "api":
            ok = bool(openai_voice.api_key(self.cfg))
            return {"mode": "api", "effective_mode": "api" if ok else None,
                    "ok": ok, "detail": "API key set. Recognition uses gpt-live-transcribe, speech uses gpt-4o-mini-tts"
                    if ok else "API key not set"}
        try:
            live_module.credentials()
            return {"mode": "subscription", "effective_mode": "subscription",
                    "ok": True, "detail": "Signed in through Codex with a ChatGPT subscription. Model: gpt-live-1-codex"}
        except Exception:
            return {"mode": "subscription", "effective_mode": None,
                    "ok": False, "detail": "Sign in with codex login"}

    def _phone_audio_stop(self):
        """«Заткнись» / новая фраза: недосинтезированное на телефон не отправлять, OpenAI — снова в динамики."""
        self._phone_token += 1
        self.gemini_tts.stop()
        if self.live:
            self.live.audio.sink = None

    def _phone_openai(self, text):
        """Голос OpenAI на телефон: звук модели перехватываем до динамиков (AudioOut.sink),
        копим по ~2.5 с и шлём кусками."""
        live = self.live
        if not live or not live.ws:
            return False
        self._phone_token += 1
        token = self._phone_token
        buf = {"pcm": [], "len": 0, "rate": 24000, "at": time.time(), "heard": False}
        lock = threading.Lock()

        def flush():
            with lock:
                if not buf["pcm"]:
                    return
                pcm = np.concatenate(buf["pcm"])
                buf["pcm"], buf["len"] = [], 0
            if token == self._phone_token:
                audio = pcm.astype(np.float32) / 32768
                self.phone.speak_audio(self._to_m4a(audio, buf["rate"]), "audio/mp4", len(audio) / buf["rate"])

        def sink(pcm, rate, channels):
            if token != self._phone_token:
                return
            mono = pcm.reshape(-1, channels)[:, 0] if channels > 1 else pcm.reshape(-1)
            loud = int(np.abs(mono).max(initial=0)) > live_module.LOUD
            # канал OpenAI шлёт звук без перерыва, и тишину тоже: берём речь и короткие паузы в ней,
            # тишину до начала и после конца — нет (иначе телефон «играет» тишину бесконечно)
            with lock:
                if loud:
                    buf["at"], buf["heard"] = time.time(), True
                elif not buf["heard"] or time.time() - buf["at"] > 0.8:
                    return
                buf["pcm"].append(mono.copy())
                buf["len"] += len(mono)
                buf["rate"] = rate
                full = buf["len"] >= rate * 2.5
            if full:
                threading.Thread(target=flush, daemon=True).start()

        def tail():                               # модель замолчала — дослать остаток и вернуть звук в динамики
            started = time.time()
            while token == self._phone_token:
                time.sleep(0.3)
                quiet = time.time() - buf["at"]
                if buf["heard"] and quiet > 1.2:
                    break                         # речь была и кончилась
                if not buf["heard"] and time.time() - started > 20:
                    break                         # модель так и не заговорила
            flush()
            if live.audio.sink is sink:
                live.audio.sink = None

        live.audio.sink = sink
        if not live.speak(text):
            live.audio.sink = None
            return False
        self.phone.until = time.time() + 3
        threading.Thread(target=tail, daemon=True).start()
        self.log.debug("Spoke on phone with OpenAI, %d characters", len(text))
        return True

    @staticmethod
    def _to_m4a(audio, rate):
        """float32 -> AAC (m4a, 64 кбит/с): по сети — в 10 раз меньше WAV."""
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            wav, m4a = os.path.join(tmp, "a.wav"), os.path.join(tmp, "a.m4a")
            sf.write(wav, audio, rate, subtype="PCM_16")
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", wav, m4a],
                           check=True, capture_output=True, timeout=30)
            with open(m4a, "rb") as f:
                return f.read()

    def _remote_voice(self):
        """Live-сессия для озвучки голосом OpenAI или None (тогда читает системный голос)."""
        return self.live if self._speaker_engine() == "openai" and not self.openai_api() else None

    def route(self):
        """Куда отправлять: orca / keys / applescript."""
        route = self.cfg.get("target", "orca")
        return route if route in ROUTES else "orca"

    def _resolve(self):
        """Маршрут для команды: (orca | keys | applescript, приложение или None).

        В «умном» способе маршрут — по приложению в фокусе (direct.smart_route).
        (None, None) — отправлять некуда, статус уже выставлен.
        """
        route = self.route()
        self.direct.type_to_previous = self.cfg.get("type_to_previous", True)
        if route != "smart":
            return route, None
        try:
            route, app = self.direct.smart_route()
        except Exception as e:
            self.log.warning("direct: %s", e)
            route, app = None, None
        if route is None:
            if not self.direct.type_to_previous and self.direct.own_front():
                self.status = t("st.own_window")       # впереди наше окно, а прыгать в прошлое запрещено
            else:
                self.status = t("st.smart_none")       # впереди не терминал — ничего не трогаем
            self.log.info(self.status)
            return None, None
        return route, app

    def custom_answer(self, data):
        """POST /api/answer {agent, text, cwd?}: ответ своего агента (хук, скрипт). Только для добавленных и включённых."""
        name = str(data.get("agent") or "").strip()
        text = str(data.get("text") or "").strip()
        agents = {a.get("name"): a for a in self.cfg.get("custom_agents") or [] if isinstance(a, dict)}
        if not name or not text:
            raise ValueError("Agent and text are required")
        if name not in agents:
            raise ValueError(f"Add the agent {name} in the Agents tab")
        if not agents[name].get("enabled", True):
            return False
        self.watcher.push(name, text, data.get("cwd"), full=data.get("full") is True)
        return True

    SOURCES_TTL = 10     # состояние источников ответов пересчитываем не чаще раза в 10 с

    def answer_sources(self):
        """Откуда Voice берёт ответы агентов: on — подключено, found — есть на Маке, но не подключено, off — нет."""
        cached = getattr(self, "_sources", None)
        if cached and time.time() - cached[0] < self.SOURCES_TTL:
            return cached[1]
        home = Path.home()

        def newest(folder, pattern):
            """Когда последний раз писался журнал (для подписи «свежий: …»)."""
            try:
                times = [p.stat().st_mtime for p in folder.rglob(pattern)]
                return time.strftime("%d.%m %H:%M", time.localtime(max(times))) if times else ""
            except Exception:
                return ""

        linked = self.cfg.get("sources") or {}
        try:
            agents = sum(len(w.get("agents", [])) for w in orca_cli.worktrees())
            orca = ("on", f"agents: {agents}")
        except Exception:
            orca = ("off", "Orca is not running")
        claude_dir, codex_dir = home / ".claude" / "projects", home / ".codex" / "sessions"
        rows = [
            {"key": "orca", "name": "Orca (agent tabs)", "state": orca[0], "detail": orca[1]},
            {"key": "claude", "name": "Claude Code logs (~/.claude/projects)",
             "state": "on" if claude_dir.is_dir() else "off",
             "detail": ("latest: " + newest(claude_dir, "*.jsonl")) if claude_dir.is_dir() else "folder not found"},
            {"key": "codex", "name": "Codex logs (~/.codex/sessions)",
             "state": "on" if codex_dir.is_dir() else "off",
             "detail": ("latest: " + newest(codex_dir, "*.jsonl")) if codex_dir.is_dir() else "folder not found"},
            {"key": "opencode", "name": "OpenCode outside Orca (~/.local/share/opencode)",
             "state": "found" if (home / ".local" / "share" / "opencode").is_dir() else "off",
             "detail": "Installed on this Mac. Logs are not supported yet" if (home / ".local" / "share" / "opencode").is_dir() else "not found"},
            {"key": "grok", "name": "Grok CLI logs (~/.grok/sessions)",
             "state": "on" if (home / ".grok" / "sessions").is_dir() else "off",
             "detail": ("latest: " + newest(home / ".grok" / "sessions", "chat_history.jsonl")) if (home / ".grok" / "sessions").is_dir() else "not found"},
        ]
        for row in rows:
            if row["key"] in ("orca", "claude", "codex", "grok") and not linked.get(row["key"], True) and row["state"] == "on":
                row["state"], row["detail"] = "found", "disabled manually"
            if row["key"] == "opencode" and row["state"] == "found":
                row["detail"] = "installed on this Mac and unsupported"
        self._sources = (time.time(), rows)
        return rows

    def answer_front(self):
        """Что впереди — для озвучки «только активное окно» (read_scope=active).

        ("orca", вкладка Orca в фокусе) | ("cwds", папки процессов вкладки Терминала/iTerm2) |
        ("any", None) — другой терминал (вкладку не узнать) | (None, None) — впереди не терминал: молчим.
        Впереди окно Voice или Goblin Remote — смотрим приложение, где были до него.
        """
        app = self.direct.front_app()
        if app is not None and self.direct.is_ours(app):
            saved = self.last_term.snapshot() or {}
            if saved.get("kind") == "orca" and self.direct.is_running(direct.ORCA_APP):
                return "orca", saved
            app = self.direct.last_app
        if app is None or app.isTerminated():
            return None, None
        bundle = app.bundleIdentifier()
        if bundle == direct.ORCA_APP:
            try:
                return "orca", orca_cli.focused_terminal()
            except Exception:
                return None, None
        if bundle in direct.TERMINAL_APPS:
            dirs = direct.front_tab_dirs(bundle)
            return ("any", None) if dirs is None else ("cwds", dirs)
        return None, None

    FRONT_LABEL_EVERY = 1.5                     # имя вкладки в фокусе — не чаще (osascript ~50 мс)

    def terminal_nick(self, tty):
        return next((x.get("nick", "") for x in self.orch.sessions() if x.get("tty") == tty), "")

    def terminal_focus(self):
        """Show input focus independently of which application covers Terminal."""
        try:
            focus = self.speech_focus()
            if not focus.get("tty"):
                return {}
            app = self.direct.front_app()
            foreground = bool(app is not None and not app.isTerminated()
                              and app.bundleIdentifier() == "com.apple.Terminal")
            return {"tty": focus["tty"], "selected": not foreground}
        except Exception:
            return {}

    def front_terminal_label(self):
        """Строка «Активный терминал»: вкладка, куда сейчас уйдёт печать и диктовка.
        Те же правила, что у _focused (способ, «Печатать в фоне», окно Voice впереди), но только смотрим:
        статус и журнал не трогаем."""
        pinned_label = self.input_focus_summary()["selected_label"]
        if pinned_label:
            return pinned_label
        now = time.monotonic()
        cached = getattr(self, "_front_label", None)
        if cached and now - cached[0] < self.FRONT_LABEL_EVERY:
            return cached[1]
        label = ""
        try:
            route, app = self.route(), None
            self.direct.type_to_previous = self.cfg.get("type_to_previous", True)
            front = self.direct.front_app()
            if route == "smart":
                route, app = self.direct.smart_route()
            if route == "orca":
                term = orca_cli.focused_terminal()
                label = f"Orca · {term['worktree']} / {term['title']}" if term else t("dash.front.nowhere")
            elif route is None:
                label = t("dash.front.nowhere")
            elif (route == "applescript" and self.cfg.get("type_in_background", False)
                  and not (front is not None and front.bundleIdentifier() in direct.TERMINALS)):
                bg = self.bg_preview()                      # терминал не впереди — печать в фоне
                label = bg["text"] if bg and bg.get("ok") else t("dash.front.nowhere")
            else:
                app = app or self.direct._pick(route)
                if app is None:
                    label = t("dash.front.nowhere")
                else:
                    name = str(app.localizedName() or "")
                    tab = direct.front_tab_name(app.bundleIdentifier())
                    label = f"{name} · {tab}" if tab else name
                    bundle = app.bundleIdentifier()
                    if bundle in direct.FRONT_TTY:
                        tty = direct._osascript(direct.FRONT_TTY[bundle]).strip()
                        nick = self.terminal_nick(tty)
                        if nick:
                            label += " · " + nick
        except Exception as e:
            self.log.debug("active terminal: %s", e)
        self._front_label = (now, label)
        return label

    def answer_route(self):
        """Чьи ответы читать (для AnswerWatcher в «умном» способе): orca — по статусам Orca, direct — по журналам.

        По приложению в фокусе; если впереди не терминал — по вкладке, куда последний раз слали клавиши."""
        try:
            self.direct.type_to_previous = self.cfg.get("type_to_previous", True)
            route, _ = self.direct.smart_route()
        except Exception:
            route = None
        if route is None:
            last = self.state.get("last_target")
            return "direct" if last and last.get("via") else "orca"
        return "orca" if route == "orca" else "direct"

    # ---------- распознавание фраз (по очереди)

    def on_phrase(self, wav, long=False, spoken=False, final=True, sid=None, weak=False):
        if self.cfg.get("audio_diagnostics"):
            if not isinstance(wav, PhraseAudio):
                wav = PhraseAudio(wav, getattr(wav, "pause_before_s", float("inf")),
                                  getattr(wav, "interrupted_voice", False))
            wav.diagnostic_end = time.time()
        # weak — короткое тихое слово (listener.py, WEAK_MS): выполняем, только если это команда
        # «слушать после сигнала»: фраза кончилась — до сигнала «напечатано» звук не распознаём
        local = (self._ear_gate("whisper", "gigaam", "apple", "grok", "gemini", "zipa") or
                 (self.openai_api() and self._ear_gate("openai"))) and not self.state["live"]
        ear = self.listener.ear_close() if final and not long and not spoken and local and not weak else None
        self.log.debug("Speech detection: phrase of %.1f s (long=%s, spoken=%s, final=%s)%s%s",
                       (len(wav) - 44) / 32000, long, spoken, final, "; waiting for the sound before recognition" if ear else "",
                       "; interrupted speech" if getattr(wav, "interrupted_voice", False) else "")
        if self.cfg.get("audio_diagnostics"):
            self.log.info("Audio diagnostics: recognizing %.3f s phrase; final=%s weak=%s sid=%s; vad=%s",
                          (len(wav) - 44) / 32000, final, weak, sid, self.cfg.get("vad"))
        self.phrases.put((wav, long, spoken, ear, sid, weak))

    def _ear_gate(self, *modes):
        """Включено «слушать после сигнала» и диктовка — одна из modes."""
        return bool(self.cfg.get("listen_after_signal")) and bool(self.listener) and self.dictation() in modes

    def _ear_after_signal(self, ear):
        """«Слушать после сигнала»: снова распознавать, когда отзвучит сигнал фразы
        (сигнала не было — команда без звука, мусор, пусто — сразу)."""
        proc = getattr(self._signal, "proc", None)
        self._signal.proc = None
        if not ear or not self.listener:
            return
        if not proc or proc.poll() is not None:
            self.listener.ear_open(ear)
            self.log.debug("Recognition resumed without a sound")
            return

        def wait():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.listener.ear_open(ear)
            self.log.debug("Recognition resumed after the sound played")
        threading.Thread(target=wait, daemon=True).start()

    def worker(self):
        """Штатный режим: Whisper слушает команды.

        В режиме OpenAI сюда попадает только звук, пока говорит наш голос (ради «заткнись»).
        """
        while True:
            item = self.phrases.get()
            if callable(item):
                item()
                continue
            wav, long, spoken, ear, sid, weak = item
            self.handling = True
            self._signal.proc = None
            try:
                self._phrase(wav, long, spoken, sid, weak)
            except Exception:
                # сбой одной фразы (печать, команда, диск) не должен убить разбор всех следующих
                if getattr(self, "_recognize_submit", None):
                    self._recognize_submit["failed"] = True
                self.log.exception("Failed to process phrase; continuing with the next phrase")
                time.sleep(0.2)
            finally:
                self.handling = False
                try:
                    self._ear_after_signal(ear)
                except Exception:
                    self.log.exception("Recognition after sound")
                    if self.listener:
                        self.listener.ear_open()     # не оставить слух «ждёт сигнал» навсегда

    _zipa_rec = None        # (команда, до какого времени): следующая фраза — образец команды для ZIPA
    ZIPA_SAMPLE_MAX_S = 3.0  # образец — короткая команда; фраза длиннее (с паузами по краям) — не образец

    def _record_zipa(self, name, wav, sid):
        """Фраза — образец команды: звуки ZIPA в настройки (zipa_templates), в терминал ничего не печатаем."""
        if sid:
            try:
                self._streamer().finish(sid, 0.5)
            except Exception:
                pass
            self._apple_final(sid, "", junk=True)      # напечатанное по ходу — стереть
        try:
            phones = [p for p in self.zipa.transcribe(wav).split() if not zipa_ipa.pauses.is_pause(p)]
        except Exception as e:
            self.log.warning("ZIPA sample recording failed: %s", e)
            return
        if not phones:
            self.log.info("ZIPA sample %s was silent and was skipped", name)
            return
        sample = " ".join(phones)
        tmpl = dict(self.cfg.get("zipa_templates") or {})
        samples = [s for s in tmpl.get(name, []) if s != sample] + [sample]
        tmpl[name] = samples[-8:]                       # последние 8 образцов
        self.save_setting("zipa_templates", tmpl)
        self.log.info("ZIPA recorded sample for command %s: %s", name, sample)

    def _phrase(self, wav, long, spoken, sid=None, weak=False):
        """Одна фраза: распознать, напечатать или выполнить команды."""
        rec = None if weak else self._zipa_rec   # короткое тихое слово — не образец команды
        if rec and time.time() < rec[1]:
            # ждём образец команды: ничего не печатаем; длинная фраза или эхо голоса — не образец, ждём дальше
            seconds = (len(wav) - 44) / 32000
            if long or spoken or seconds > self.ZIPA_SAMPLE_MAX_S:
                self.log.info("ZIPA skipped %.1f s phrase; waiting for a short %s command", seconds, rec[0])
                return
            self._zipa_rec = None
            return self._record_zipa(rec[0], wav, sid)
        if rec:
            self._zipa_rec = None
        if self._mic_paused() or (weak and self.speech_only()):
            return
        phrase_mode = self.dictation()
        backup = self._backup_start(wav) if not long and not weak and not self.mic_muted() and (not spoken or not self.echo_now()) else None
        try:
            started = time.time()
            text = None
            if sid:     # диктовка «Apple» / «Grok»: окончательный текст уже распознаётся потоком (Whisper не нужен)
                text = self._streamer().finish(sid, float(self.cfg.get("apple_final_wait_s", 4)))
            if text is None:
                text = self.transcribe(wav)
            if isinstance(wav, PhraseAudio):
                wav.diagnostic_ipa = text if self.dictation() == "zipa" else None
            self.log.debug("Recognized in %.2f s (%s): %r", time.time() - started, self.dictation(), text)
            if getattr(wav, "diagnostic_end", None):
                end = wav.diagnostic_end
                self.log.info("heard: %r [audio:%.6f:%.6f]", text,
                              end - (len(wav)-44)/32000 - .4, end + .2)

        except Exception as e:
            if getattr(self, "_recognize_submit", None):
                self._recognize_submit["failed"] = True
            self.log.warning("%s: %s", self.dictation(), e)
            return
        if self.dictation() != phrase_mode:
            if sid:
                self._apple_close(sid)
            return
        barge_backup = backup
        if getattr(wav, "interrupted_voice", False):
            if self._barge_control(text, True, sid=sid):
                return
            if self.dictation() == "zipa":
                barge_backup = backup
                if self._barge_control(text, True, sid=sid, backup=barge_backup):
                    return
        if weak and (self.speech_only() or not self._only_commands(text, getattr(wav, "pause_before_s", float("inf")))):
            self.log.debug("Skipped short quiet word that was not a command: %r", text)
            return
        if self.mic_muted():
            if sid:
                self._apple_final(sid, "", typed_only=True)
            self._wake(text, long or spoken, pause_before_s=getattr(wav, "pause_before_s", float("inf")))
            return
        # защиты от эха нет (Эхо: выкл / авто в наушниках): речь поверх голоса — тоже ввод (голос при этом замолкает)
        barge = spoken and not self.echo_now()
        typing = (self.dictation() in ("whisper", "gigaam", "apple", "grok", "gemini", "zipa") or self.stream_mode() == "openai" or (self.dictation() == "openai" and self._json_backup_enabled())) \
            and not long and (not spoken or barge) and not self.state["live"]
        if sid and not typing:
            self._apple_final(sid, "", typed_only=True)    # не ввод (звучал наш голос) — напечатанное по ходу стираем
        if not text and not typing:
            return

        # фраза сразу после озвучки: в неё переносятся последние 0,75 с голоса — хвост ответа из колонок не печатаем
        if typing and not spoken and text and self.cfg.get("echo_filter", True) and self.speaker.text \
                and self.speaker.quiet_for() < ECHO_AFTER_S and not self._echo_free():
            if self.dictation() == "zipa":
                if zipa_ipa.is_echo(text, self.speaker.text):
                    self.log.info("Speech echo skipped: %s", text)
                    if sid:
                        self._apple_final(sid, "", typed_only=True)
                    return
            else:
                text = self._drop_echo(text)
                if not text:
                    if sid:
                        self._apple_final(sid, "", typed_only=True)
                    return

        if typing and spoken:
            if "stop" in self._commands(text, long=True):
                if sid:
                    self._apple_final(sid, "", typed_only=True)
                self.fire(["stop"])                  # «заткнись» поверх голоса — только остановить
                return
            text = self._drop_echo(text)
            if not text or self.is_junk(text):
                if sid:
                    self._apple_final(sid, "", typed_only=True)
                return
            self.reader.stop()
            self.speaker.stop()
            self.log.info("Speech stopped because you spoke over it")

        # голосовой ввод: печатаем (если не звучал наш голос и это не режим OpenAI)
        if typing and sid:
            junk = bool(text) and self.is_junk(text)
            if junk:
                self.log.info(t("log.junk"), text)
                text = ""
            if text:
                self.heard = text
                self.log.info(t("log.heard_typing"), text)
            self._apple_final(sid, text, junk=junk, backup=backup)
            return
        if typing:
            if text and self.is_junk(text):
                self.log.info(t("log.junk"), text)
                if backup is None or not self._keyword_input():
                    return
                text = ""
            # текст потерял Enter в конце — ищем по звуку (поверх голоса не ищем: в звуке эхо озвучки)
            tail_cmd = None if spoken or self.speech_only() else self.hear_tail(wav, text)
            if not text and not tail_cmd and not (backup is not None and self._keyword_input()):
                return
            if text:
                self.heard = text
                self.log.info(t("log.heard_typing"), text)
            self.handle_speech(text, tail_cmd, pause_before_s=getattr(wav, "pause_before_s", float("inf")),
                               backup=backup)
            return

        commands = self._commands(text, long or (spoken and self.echo_now()), pause_before_s=getattr(wav, "pause_before_s", float("inf")))
        if commands and time.time() < self.ignore_until:
            self.log.info(t("log.skipped_after_mode"), text)
            return
        if not long:
            self.heard = text
        self.log.info(t("log.heard_long" if long else "log.heard"), text, commands or "—")
        if commands:
            self.fire(commands)

    # ---------- диктовка «Apple»: печать по ходу речи

    def openai_api(self):
        """OpenAI — по ключу API (а не по подписке через сессию Codex)."""
        return openai_voice.api_mode(self.cfg)

    def stream_mode(self):
        """Диктовка потоковым распознавателем: apple / grok / openai (по ключу) — или None."""
        mode = self.dictation()
        if mode in ("apple", "grok", "gemini") or (mode == "openai" and self.openai_api()):
            return mode
        return None

    HINT_WORDS = 3          # из каждой команды распознавателю в подсказку — только первые слова (главные)

    def hint_words(self, key):
        """Подсказка распознавателю (Grok, OpenAI, Apple): первые HINT_WORDS слов каждой включённой команды,
        слова Enter — первыми, за ними свои слова (hint_extra). Остальные варианты слов — для разбора фразы, не для подсказки."""
        cmds = self.cfg.get("commands", {})
        order = ["enter"] + [n for n in cmds if n != "enter"]
        out = []
        for name in order:
            c = cmds.get(name) or {}
            if c.get("enabled", True):
                out += [w for w in c.get(key, []) if w][:self.HINT_WORDS]
            if name == "enter":
                out += self.extra_hints()     # свои слова — сразу после Enter, чтобы не обрезались лимитом
        excluded = {str(w).casefold() for w in self.cfg.get("hint_excluded", [])}
        return [w for w in dict.fromkeys(out) if w.casefold() not in excluded][:HINT_LIMIT]

    def hints_info(self):
        """Для окна («Связка»): слова-подсказки, встроенная инструкция OpenAI и правило языка к ней."""
        lang = whisper_language(self.cfg)
        return {
            "hints": self.hint_words("words_" + self.cfg.get("ui_language", "en")),
            "hint_limit": HINT_LIMIT,
            "live_prompt_default": live_module.PROMPT,
            "live_rule": live_module.LANG_RULES.get(lang, ""),
        }

    def extra_hints(self):
        """Свои слова-подсказки из «Связки» (hint_extra): через запятую или с новой строки."""
        text = str(self.cfg.get("hint_extra") or "")
        return [w.strip() for w in re.split(r"[,\n;]+", text) if w.strip()]

    def _streamer(self):
        """Streamingовый распознаватель выбранной диктовки: Grok, OpenAI (ключ) или Apple."""
        return {"grok": self.grok, "openai": self.oa, "gemini": self.gemini}.get(self.stream_mode(), self.apple)

    def _gemini_voice_error(self, error):
        self.status = "Gemini: " + error
        self.reader.stop()  # Не отмечать несостоявшуюся озвучку прочитанной.

    def _cloud_voice(self):
        """Озвучка через HTTP: Grok, или OpenAI по ключу API; иначе None."""
        reading = self._speaker_engine()
        if reading == "gemini":
            return self.gemini_tts
        if reading == "grok":
            return self.grok_tts
        if reading == "openai" and self.openai_api():
            return self.oa_tts
        return None

    def _sync_apple(self):
        """Диктовка «Apple» или «Grok» выбрана — запустить распознаватель и пустить в него звук фраз; иначе — выключить."""
        def run():
            with self._apple_sync:
                mode = self.stream_mode()
                if mode != "apple":
                    self.apple.stop()
                if mode != "grok":
                    self.grok.stop()
                if mode != "openai":
                    self.oa.stop()
                if mode != "gemini":
                    self.gemini.stop()
                if not mode:
                    if self.listener:
                        self.listener.stream = None
                    return
                lang = whisper_language(self.cfg)
                if lang == "auto":
                    lang = self.cfg.get("ui_language", "en")
                # слова команд языка окна — подсказка распознавателю
                key = "words_" + self.cfg.get("ui_language", "en")
                hints = self.hint_words(key)                 # по 3 главных слова каждой команды
                try:
                    if mode == "apple":
                        self.apple.ensure(lang, bool(self.cfg.get("apple_punctuation", True)))
                        self.apple.set_hints(hints)
                    else:
                        self._streamer().ensure(lang, hints)
                except Exception as e:
                    if self.listener:
                        self.listener.stream = None
                    self.status = t("st.%s_error" % ("grok" if mode == "openai" else mode), e).replace("Grok", "OpenAI" if mode == "openai" else "Grok")
                    self.log.warning(self.status)
                    return
                if self.listener and self.stream_mode() == mode:
                    self.listener.stream = self._streamer()
        threading.Thread(target=run, daemon=True).start()

    def _apple_typer(self):
        """Streaming печати: текст по ходу речи (накопилось — берём последний текст фразы)."""
        while True:
            sid, text = self.apple_q.get()
            try:
                while True:
                    try:
                        nsid, ntext = self.apple_q.get_nowait()
                    except queue.Empty:
                        break
                    if nsid != sid:
                        self._apple_partial(sid, text)
                    sid, text = nsid, ntext
                self._apple_partial(sid, text)
            except Exception as e:
                # поток печати по ходу речи живёт до выхода: сбой одного куска — в журнал, дальше печатаем
                self.log.warning("Apple: %s", e)
                self.log.debug("Apple live typing failed", exc_info=True)
                time.sleep(0.1)

    def _apple_partial(self, sid, text):
        """Текст по ходу речи -> на экран. live — как штатная диктовка macOS: сразу и с исправлениями;
        stable — только устойчивые слова, без исправлений; phrase — ничего (целой фразой в конце)."""
        listener = getattr(self, "listener", None)
        if listener and (getattr(listener, "_barge_active", False) or sid in getattr(listener, "_interrupted_sids", ())):
            return
        if self.cfg.get("zipa_json") or self._keyword_input():
            # JSON и команды со словом-ключом обрабатываются после распознавания всей фразы.
            return
        mode = self.cfg.get("apple_mode", "live")
        if mode == "phrase" or not self.stream_mode() or self.state["live"] or self.mic_muted():
            return
        with self.apple_lock:
            if sid in self.apple_done:
                return
            now = time.time()
            self._apple_prune(now)
            first = self.apple_order[0] if self.apple_order else None
            if first and first[0] != sid:
                return                   # раньше начатая фраза не закончена — эту напечатаем после неё (текст копится)
            if first:
                first[1] = now
            st = self.apple_st
            if not st or st["sid"] != sid:
                st = self.apple_st = {"sid": sid, "typed": "", "term": None, "frozen": False, "seen": [], "at": 0.0}
            if st["frozen"]:
                return
            if self.listener and self.listener.hold and self.listener.hold():
                st["frozen"] = True      # говорит наш голос — по ходу не печатаем, разберётся конец фразы
                return
            now = time.time()
            if mode == "stable":
                desired = self._apple_stable(st, text, now)
                if desired is None:
                    return
            else:
                every = float(self.cfg.get("apple_live_every_s", 0))
                if every and now - st["at"] < every:
                    return               # обновляем экран не чаще, чем раз в every с
                desired = " ".join(text.split())
            self._apple_apply(st, self._apple_safe(st, desired))
            st["at"] = now

    def _apple_stable(self, st, text, now):
        """«Только устойчивые слова»: слово печатаем, когда оно не менялось apple_stable_lag_s и за ним
        уже есть apple_hold_words слов. Напечатанное не правим. None — печатать нечего."""
        words = text.split()
        seen = st["seen"]
        for i, word in enumerate(words):
            if i < len(seen) and seen[i][0] == word:
                continue
            del seen[i:]                 # слово сменилось — дальше всё заново
            seen.append([word, now])
        del seen[len(words):]
        hold = int(self.cfg.get("apple_hold_words", 1))
        lag = float(self.cfg.get("apple_stable_lag_s", 0))
        ready = 0
        for i, (_, since) in enumerate(seen):
            if i >= len(words) - hold or now - since < lag:
                break
            ready = i + 1
        done = st["typed"].split()
        if ready <= len(done):
            return None
        new = words[len(done):ready]
        if not self.speech_only() and any(kind != "text" for kind, _ in self.matcher.segments(" ".join(new))):
            st["frozen"] = True          # слово команды — остальное разберёт конец фразы
            return None
        return st["typed"] + " ".join(new) + " "

    def _apple_start(self, sid):
        """Детектор речи начал фразу в потоковом распознавателе: печатать её — после всех раньше начатых."""
        with self.apple_lock:
            self.apple_order.append([sid, time.time()])

    def _apple_close(self, sid):
        """Фраза закончена: убрать из очереди печати."""
        with self.apple_lock:
            self.apple_done.add(sid)
            listener = getattr(self, "listener", None)
            if listener:
                getattr(listener, "_interrupted_sids", set()).discard(sid)
            self.apple_order = [item for item in self.apple_order if item[0] != sid]
            if self.apple_st and self.apple_st["sid"] == sid:
                self.apple_st = None

    def _apple_prune(self, now):
        """Вызывать под apple_lock. Ранняя фраза давно молчит и итога нет (сбой распознавания) — не ждём её."""
        while self.apple_order and now - self.apple_order[0][1] > APPLE_STALE_S:
            sid = self.apple_order.pop(0)[0]
            self.log.warning("Live typing: phrase %s incomplete after %d s; proceeding to the next phrase", sid, APPLE_STALE_S)
            self.apple_done.add(sid)
            if self.apple_st and self.apple_st["sid"] == sid:
                self.apple_st = None

    def _apple_drop(self, sid):
        """Фразу забыли (итога не будет): напечатанное по ходу остаётся, следующая фраза печатается сразу."""
        self._apple_close(sid)

    def _apple_safe(self, st, desired):
        """Правка без потерь: стереть можно только хвост напечатанного (до APPLE_MAX_FIX characters).
        Распознаватель переписал больше (начал фразу заново, оборвал кусок) — напечатанное оставляем
        и только допечатываем слова сверх него."""
        typed = st["typed"]
        erase = len(typed) - len(os.path.commonprefix([typed, desired]))
        if erase <= APPLE_MAX_FIX:
            return desired
        done = typed.split()
        rest = desired.split()[len(done):]
        self.log.info("Live correction to delete %d characters was cancelled", erase)
        safe = typed.rstrip() + (" " + " ".join(rest) if rest else "")
        return safe + " " if desired.endswith(" ") else safe

    def _apple_apply(self, st, desired):
        """Привести напечатанное к desired: стереть отличающийся хвост и допечатать новый."""
        typed = st["typed"]
        common = 0
        for a, b in zip(typed, desired):
            if a != b:
                break
            common += 1
        erase, add = len(typed) - common, desired[common:]
        if not erase and not add:
            return
        term = st["term"] or self._focused()
        if not term:
            st["frozen"] = True
            return
        st["term"] = term
        try:
            # итог прошлого куска и печать нового по ходу — не вперемешку
            with self.apple_type_lock, self.input_lock:
                self._guard_input(term)
                if term.get("via"):
                    self.direct.edit(term, erase, add)
                else:
                    orca_cli.send_edit(term["handle"], erase, add)
                self._unsent_add(term, len(add) - erase)
                st["typed"] = desired
                self.typed_pending = bool(desired)
        except Exception as e:
            self._input_error(term, e)
            st["frozen"] = True
            return
        self.log.debug("Apple live typing: removed %d, added %r", erase, add)

    def _apple_final(self, sid, text, typed_only=False, junk=False, backup=None):
        """Конец фразы «Apple»: окончательный текст. Напечатанное по ходу — правим до него,
        команды-клавиши в конце («…, рекс») жмём после текста; прочие команды — обычным путём."""
        with self.apple_lock:
            self.apple_done.add(sid)
            if len(self.apple_done) > 500:
                self.apple_done = {sid}
            st = self.apple_st if self.apple_st and self.apple_st["sid"] == sid else None
            for item in self.apple_order:
                if item[0] == sid:
                    item[1] = time.time()    # итог в работе — фраза жива
        try:
            self._apple_finish(st, text, typed_only, junk, backup=backup)
        finally:
            self._apple_close(sid)           # фраза закончена (и Enter нажат) — можно печатать следующую

    def _apple_finish(self, st, text, typed_only, junk=False, backup=None):
        """Итог фразы «Apple» на экран (см. _apple_final). junk — мусор («Субтитры…»): напечатанное стираем."""
        old_target = st.get("term") if st else None
        if old_target and old_target.get("input_epoch", self.input_epoch) != self.input_epoch:
            return
        typed = st["typed"] if st else ""
        if junk and typed:
            self._apple_apply(st, "")
            return
        if not text and typed.strip() and not typed_only:
            # окончательный текст пустой (Apple оборвал кусок длинной речи) — напечатанное по ходу не стираем
            self.log.info("Apple final phrase is empty; keeping the text already typed: %s", typed.strip())
            text = typed.strip()
        if typed_only or not typed:
            if typed:
                # напечатано до того, как зазвучал голос, — это ваши слова: не стираем
                self.log.info("Phrase is not for typing; keeping the text already typed: %s", typed.strip())
            if not typed_only and (text or (backup is not None and self._keyword_input())):
                self.handle_speech(text, backup=backup)  # без страховки сохраняется обычная потоковая печать
            return

        if self.speech_only():
            segments = [("text", text)] if text else []
        else:
            skip = (() if self.matcher.short(text) else self.matcher.LONG_SKIP) + self.matcher.mode_skip(text)
            segments = self.matcher.segments(text, skip=skip) if text else []
        head = 0
        while head < len(segments) and segments[head][0] == "text":
            head += 1
        tail = segments[head:]
        if any(kind not in KEY_COMMANDS for kind, _ in tail):
            self._apple_apply(st, "")                # переключение, режимы, команда посреди фразы
            if not st.get("frozen"):
                self.handle_speech(text)
            return

        words = " ".join(value for _, value in segments[:head])
        if self.cfg.get("apple_mode", "live") == "stable":
            # без исправлений: допечатываем то, чего ещё нет
            done = typed.split()
            rest = words.split()[len(done):]
            desired = typed + (" ".join(rest) + " " if rest else "")
        else:
            desired = self._apple_safe(st, words + " " if words else "")
        self._apple_apply(st, desired)
        if st.get("frozen"):
            return
        term = st["term"]
        if desired.strip() and term:
            self.phone.said(desired.strip())
            self.state["last_target"] = term
            if not term.get("jonny"):
                self.last_term.note(term)
            self.play("text", 0.5)                   # сигнал: фраза в терминале
            self.log.info(t("log.typed"), desired.strip(), self._target_label(term))
        typed_at = time.time()
        for kind, _ in tail:
            if term:
                time.sleep(max(0.0, ENTER_AFTER_TEXT - (time.time() - typed_at)))
                if self.speech_only():
                    break
                self._send_key(term, kind)
                if kind == "enter":
                    self.typed_pending = False

    def user_talking(self):
        """Человек сейчас диктует: детектор слышит голос, голос был меньше hold_pause_s назад
        или фраза ещё распознаётся / ждёт очереди. Тогда ответы агентов не читаем, а откладываем."""
        if self.handling or not self.phrases.empty():
            return True
        lis = self.listener
        if not lis or (lis.paused and not lis.remote):
            return False
        if lis.remote and self.phone.mode == "text":
            return False        # tablet распознаёт сам и звук не шлёт — детектор мог застрять на прошлой фразе
        pause = float(self.cfg.get("hold_pause_s", 2))
        return lis.in_speech or time.time() - lis.last_voice < pause

    def fuzzy_commands(self):
        """Команды, для которых сейчас работает «нечёткий слух»: галочка включена и диктовка — GigaAM."""
        if self.speech_only() or not (self.cfg.get("fuzzy_ear") and self.dictation() == "gigaam"):
            return []
        return list(fuzzy.FUZZY_COMMANDS)

    def hear_tail(self, wav, text):
        """«Нечёткий слух» (диктовка GigaAM): текст не нашёл команду из fuzzy.FUZZY_COMMANDS —
        проверяем по звуку, не сказано ли её слово в самом конце фразы. Возвращает команду или None."""
        if not self.fuzzy_commands():
            return None
        targets = {self.matcher.ALIASES.get(n, n) for n in fuzzy.FUZZY_COMMANDS}
        if text and any(kind in targets for kind, _ in self.matcher.segments(text)):
            return None                           # текст сам нашёл команду — как обычно
        try:
            pcm = np.frombuffer(wav[44:], dtype=np.int16).astype(np.float32) / 32767
            found = self.ear.best(pcm, fuzzy.command_words(self.cfg))
        except Exception as e:
            self.log.warning(t("log.fuzzy_error"), e)
            return None
        if not found:
            return None
        name, word, prob = found
        limit = float(self.cfg.get("fuzzy_threshold", 0.2))
        if prob >= limit:
            self.log.info(t("log.fuzzy_hit"), text, name, word, prob, limit)
            return self.matcher.ALIASES.get(name, name)
        if prob >= limit / 4:                     # почти сработало — видно, куда двигать порог
            self.log.info(t("log.fuzzy_near"), text, name, word, prob, limit)
        return None

    @staticmethod
    def _echo_key(word):
        """Слово для сравнения с озвучкой: без знаков, первые 5 букв (распознаватель путает окончания)."""
        return re.sub(r"\W+", "", word.lower().replace("ё", "е"))[:5]

    def _drop_echo(self, text):
        """«Любая диктовка» поверх голоса: убрать слова, которые сейчас читает голос (эхо из динамиков).
        Своих слов осталось меньше ECHO_MIN_WORDS — вся фраза эхо, возвращаем ''."""
        if not self.cfg.get("echo_filter", True) or self._echo_free():
            return text
        if self.dictation() == "zipa":
            # IPA — отдельные звуки, а не слова: эхо — только если вся фраза похожа на кусок озвучки
            if zipa_ipa.is_echo(text, self.speaker.text):
                self.log.info("Speech echo skipped: %s", text)
                return ""
            return text
        said = {self._echo_key(w) for w in self.speaker.text.split()}
        kept = [w for w in text.split() if self._echo_key(w) not in said]
        own = [w for w in kept if len(self._echo_key(w)) >= 3]
        if len(own) < ECHO_MIN_WORDS:
            self.log.info("Speech echo skipped: %s", text)
            return ""
        if len(kept) < len(text.split()):
            self.log.debug("Removed speech echo: %r -> %r", text, " ".join(kept))
        return " ".join(kept)

    def is_junk(self, text):
        """Шум, а не речь — для всех распознавателей: один звук или одни гласные («а», «э», «a a»);
        и фразы, которые Whisper «слышит» в шуме: «Субтитры…», «Спасибо за просмотр», капс."""
        if self.dictation() == "zipa":
            return zipa_ipa.is_noise(text)          # IPA: буквы — звуки, словари Whisper тут ни при чём
        letters = [c for c in text if c.isalpha()]
        if len(letters) < 2 or all(c.lower() in VOWELS for c in letters):
            return True
        if len(letters) >= 3 and all(c.isupper() for c in letters):
            return True
        norm = " ".join(re.sub(r"[^\w\s]+", " ", text.lower().replace("ё", "е")).split())
        if norm in JUNK_EXACT or any(m in norm + " " for m in JUNK_MARKERS):
            return True
        if len(norm.split()) > 8:
            return False
        return any(norm.startswith(item) for item in self.cfg.get("dictation_ignore", []) if item)

    def _commands(self, text, long=False, pause_before_s=float("inf")):
        """Команды во фразе, которую не печатаем. ZIPA — по звучанию фонем (слов в IPA нет), иначе — по словам."""
        if self.speech_only():
            return []
        if self.dictation() != "zipa":
            return self.matcher.commands(text, long)
        if long:                                   # кусок длинной речи / поверх голоса: только «заткнись»
            if not self.cfg.get("commands", {}).get("stop", {}).get("enabled", True):
                return []
            return ["stop"] if zipa_ipa.has_command(text, self.matcher, "stop", self.cfg.get("zipa_templates"), pause_before_s) else []
        return zipa_ipa.split_commands(text, self.matcher, self.cfg.get("zipa_templates"), pause_before_s)[1]

    def _only_commands(self, text, pause_before_s=float("inf")):
        """Фраза — только команды («рекс», «удали фразу»…), без текста для печати: так разбираем короткое тихое слово."""
        if not text:
            return False
        if self.dictation() == "zipa":
            segments = zipa_ipa.segments(text, self.matcher, self.cfg.get("zipa_templates"), pause_before_s)
        else:
            segments = self.matcher.segments(text)
        words = lambda v: re.sub(r"[\W_]+", "", str(v or ""))
        return any(k != "text" for k, _ in segments) and not any(k == "text" and words(v) for k, v in segments)

    def handle_speech(self, text, tail_cmd=None, words=False, pause_before_s=float("inf"), backup=None):
        """Фраза для ввода: текст печатаем во вкладку в фокусе, команды выполняем по порядку.

        tail_cmd — команда, услышанная «нечётким слухом» в конце фразы (её нет в тексте).
        words — фраза обычными словами (текст с телефона), даже если диктовка на Маке — ZIPA."""
        source_mode = self.dictation()
        term = None
        typed_at = 0.0
        last = None
        typed = []                       # что напечатали — в ленту чата на tabletе
        # «переключи терминал» — команда только в короткой фразе (не длиннее max_words):
        # тогда выполняем одно переключение, остальные слова фразы не печатаем.
        # В длинной фразе эти слова — обычный текст.
        speech_only = self.speech_only()
        if not speech_only:
            if source_mode == "zipa" and not words:
                segments = self._zipa_segments(text, pause_before_s)
                segments = self._probe_wake(text, segments, pause_before_s)
            else:
                skip = (() if self.matcher.short(text) else self.matcher.LONG_SKIP) + self.matcher.mode_skip(text)
                segments = self.matcher.segments(text, skip=skip)
            if tail_cmd:
                segments.append((tail_cmd, None))
            if not words:
                segments = self._backup_wake(segments, backup, pause_before_s)
                segments = self._backup_commands(segments, backup)
                segments = self._backup_rex(segments, backup)
            speech_only = self.speech_only()  # режим мог смениться, пока ждали страховку
        if speech_only:
            segments = [("text", text)] if text and text.strip() else []
        self.log.debug("Parsed phrase: %s", segments)
        switch = None if speech_only else next((kind for kind, _ in segments if kind in self.matcher.SWITCHES), None)
        if switch:
            dropped = " ".join(v for k, v in segments if k == "text")
            self.release_pending = False         # «держи и говори»: Enter после этой фразы не нужен
            if switch == "active":
                self._return_terminal()          # «Активный терминал»: назад в последний терминал
            elif switch == "project":
                self._switch_project()           # «Переключи проект»: следующий проект Orca
            else:
                self._switch_tab(int(switch[3:]) if switch.startswith("tab") else None,
                                 direction=-1 if switch == "previous" else 1)
            self.log.info("Switching terminal%s", f" (skipped typing: “{dropped}”)" if dropped else "")
            return
        for kind, value in segments:
            if kind != "text" and self.speech_only():
                continue
            last = kind
            if kind in ("jonny", "amigo"):
                continue
            if kind == "wake":
                self.kw_awake = True             # «дружище»: режим «по ключевому слову» — диктовка проснулась
                self.kw_at = time.time()
                self.log.info("Wake word activated dictation")
                if time.time() - self._wake_rung > 8:
                    self._wake_signal()          # колокольчик «слушаю» (если не прозвенел на лету); текст фразы печатается сразу
                continue
            if kind == "mic_on":
                self._mic_on_cmd()               # «включи диктовку»: только микрофон, окна не трогаем
                continue
            if kind == "stop":
                self.reader.stop()
                self.speaker.stop()
                self.watcher.clear_held()
                self._work_stop("Stop command")
                self.phone.command(t("chat.stop"))
                self.log.info(t("log.voice_stopped"))
                continue
            if kind in MODE_COMMANDS:
                if self.mode_command(kind):
                    return          # после смены режима остальное не печатаем
                continue            # режим уже такой — просто пропускаем слово
            if kind in ("switch", "previous"):
                term = self._switch_tab(direction=-1 if kind == "previous" else 1) or term   # дальше печатаем уже в новую вкладку
                continue
            if kind.startswith("lenta"):
                self.lenta_read(int(kind[5:]))      # «сборщик первое»: прочитать сообщение ленты
                continue
            term = term or self._focused()
            if not term:
                return
            if kind == "text":
                if self._keyword_input():
                    if self.kw_awake and time.time() - self.kw_at > self.KW_SLEEP_S:
                        self.kw_awake = False    # долго молчали: Enter мог уйти мимо нас — снова ждём слово-ключ
                        self.log.info("Dictation paused after %d s of silence", self.KW_SLEEP_S)
                    if not self.kw_awake:
                        continue                 # ждём слово-ключ: в терминал ничего не идёт
                    self.kw_at = time.time()
                try:
                    agent = self._term_agent(term)
                    output, as_json = self._format_dictation(value, backup, from_phone=words)
                    if self.dictation() != source_mode:
                        return  # режим сменился, пока ждали страховку: старую фразу не допечатываем
                    with self.input_lock:
                        if as_json:
                            self._send_text(term, output + " ", preserve_whitespace=True)
                        else:
                            self._send_text(term, output + " ")
                        self.typed_pending = True
                        self.state["last_target"] = term
                    backup = None                       # страховка — только в первый кусок фразы
                    typed_at = time.time()
                    typed.append(value)
                    self.phone.said(value)         # в ленту чата на tabletе — до команд, что идут следом
                    if not term.get("jonny"):
                        self.last_term.note(term)  # терминал Джони «последним» не считаем
                    self.play("text", 0.5)     # тихий сигнал: фраза в терминале
                    self.log.info(t("log.typed"), value, self._target_label(term, agent))
                except Exception as e:
                    self._input_error(term, e)
                    return
            else:   # space / double / enter / clear
                # текст только что напечатан: Enter сразу за ним терминал (Claude Code) примет за часть вставки
                # и сделает перевод строки — даём паузу
                if typed_at and time.time() - typed_at < ENTER_AFTER_TEXT:
                    time.sleep(ENTER_AFTER_TEXT - (time.time() - typed_at))
                if self.speech_only():
                    continue
                self._send_key(term, kind)
                if kind == "enter":
                    self.typed_pending = False
        # «держи и говори»: кнопку отпустили — фраза уходит сама
        if self.release_pending and typed_at and last != "enter" and term:
            self.release_pending = False
            time.sleep(max(0.0, ENTER_AFTER_TEXT - (time.time() - typed_at)))
            self._send_key(term, "enter")
            self.typed_pending = False

    def _mic_on_cmd(self):
        """«Включи диктовку»: микрофон включён, сигнал «слушаю»; режим Джони и окна — как были."""
        if not self.mic_on():
            self.set_mic(True)
        self.play("mic_on")

    def _clear_line(self, term):
        """Стереть строку ввода терминала (как «удали фразу», но без сигнала и записи в ленту)."""
        try:
            self._press(term, "clear")
            self.log.info("Cleared input → %s", self._target_label(term))
        except Exception as e:
            self._input_error(term, e)

    # ---------- поиск по всему подробному журналу (страница orca-voice-full.html)

    log_token = secrets.token_hex(8)      # метка для /api/log/search: страница из файла ходит к нам, чужие сайты — нет
    LOG_SEARCH_MAX = 1000                 # сколько найденных записей показывать (свежие)

    LOG_PAGE = 500                        # записей на странице и за одно «Догрузить ещё»

    @staticmethod
    def _log_records():
        """Записи подробного журнала свежими сверху: сегодня, потом дни из archive/ (по файлу за раз)."""
        files = [FULL_LOG] + sorted(LOG_ARCHIVE.glob(f"{FULL_LOG.stem}-*{FULL_LOG.suffix}"), reverse=True)
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            yield from reversed(log_html.records(lines))

    def log_page(self, offset, limit):
        """Строки записей [offset, offset+limit) свежими сверху и есть ли записи дальше."""
        out, more = [], False
        for i, rec in enumerate(self._log_records()):
            if i < offset:
                continue
            if i >= offset + limit:
                more = True
                break
            out.extend(rec)
        return out, more

    def log_more(self, offset):
        """«Догрузить ещё» на странице журнала: следующие LOG_PAGE записей HTML-строками."""
        lines, more = self.log_page(max(0, offset), self.LOG_PAGE)
        return {"html": log_html.body(lines, self.cfg.get("ui_language", "en") == "en"), "more": more}

    def log_search(self, q):
        """Записи с q (без учёта регистра) — сегодня и все дни из archive/, свежие сверху."""
        q = q.strip().lower()
        en = self.cfg.get("ui_language", "en") == "en"
        hits, total = [], 0
        for rec in self._log_records():
            if q and q in "\n".join(rec).lower():
                total += 1
                if total <= self.LOG_SEARCH_MAX:
                    hits.extend(rec)
        shown = min(total, self.LOG_SEARCH_MAX)
        if en:
            title = f"Found in the whole log: {total}" + (f" (latest {shown} shown)" if total > shown else "")
        else:
            title = f"Matches in the full log: {total}" + (f" (showing the latest {shown})" if total > shown else "")
        return {"title": title, "count": total, "html": log_html.body(hits, en)}

    # ---------- команды

    def recognize_and_send(self):
        """Send after the audio/recognition FIFO boundary, never before text insertion."""
        if not self.listener or self.state.get("live"):
            raise RuntimeError("This command requires standard dictation mode")
        if getattr(self, "_recognize_submit", None):
            raise RuntimeError("Transcription and delivery are already pending")
        term = self._focused()
        if not term:
            raise RuntimeError("No terminal is available for delivery")
        request = {"done": threading.Event(), "failed": False, "epoch": self.input_epoch,
                   "term": term, "sent": False, "cancelled": False}
        self._recognize_submit = request
        def submit():
            try:
                if request["cancelled"] or request["failed"] or request["epoch"] != self.input_epoch:
                    raise RuntimeError("Delivery was cancelled. Recognition or input failed, or the terminal changed")
                last_target = self.state.get("last_target")
                if last_target and self._unsent_key(last_target) != self._unsent_key(term):
                    raise RuntimeError("Text went to a different terminal. Automatic Enter was cancelled")
                if not request["sent"] and self.typed_pending:
                    time.sleep(ENTER_AFTER_TEXT)
                    self._send_key(term, "enter")
                    if request["failed"]:
                        raise RuntimeError("Could not send the text")
                    self.typed_pending = False
                self.log.info("Recognize and send: queue processed")
            except Exception as e:
                request["failed"] = True
                self.log.warning("Recognize and send: %s", e)
            finally:
                request["done"].set()
        try:
            self.listener.finish_for_submit(lambda: self.phrases.put(submit))
            try:
                self.play("recognize_send", 0.2)
            except Exception:
                self.log.exception("Could not play the quiet confirmation sound")
            if not request["done"].wait(180):
                request["cancelled"] = True
                raise RuntimeError("Recognition timed out. Text was saved. Automatic Enter was cancelled")
            if request["failed"]:
                raise RuntimeError("Recognition or delivery is still pending. The next button action was cancelled")
        finally:
            self._recognize_submit = None

    def media_action(self, action):
        self.log.info("Media button: %s", action)
        if action == "hush":
            return self.action("hush")
        if action == "normal_mode":
            return self.media_keys.set_mode("normal")
        if action in ("extra1_mode", "extra2_mode"):
            return self.media_keys.set_mode(action.removesuffix("_mode"))
        if action == "terminal_mode":
            return self.media_keys.toggle_terminal_mode()
        if action == "recognize_send":
            return self.recognize_and_send()
        if action == "start_send":
            awake = self.mic_on() and (not self._keyword_input() or self.kw_awake)
            action = "send_pause" if awake else "start"
        if action == "start":
            if self.paused():
                self.set_paused(False)
            self.set_mic(True)
            self.kw_awake, self.kw_at = True, time.time()
            self.play("mic_on")
        elif action in ("commands", "mic_off"):
            self.kw_awake = False
            self.set_mic(False, mute=action == "commands")
            self.play("mic_mute" if action == "commands" else "mic_off")
        elif action in ("send", "send_pause", "clear", "next", "previous"):
            self.fire([{"send": "enter", "send_pause": "enter", "clear": "clear", "next": "switch", "previous": "previous"}[action]])
            if action == "send_pause":
                self.set_mic(False, mute=True)

    def fire_async(self, commands):
        threading.Thread(target=self.fire, args=(commands,), daemon=True).start()

    def play(self, sound, volume=1.0):
        # Диагностика голосового удаления: ручное прослушивание обходит этот метод.
        if sound == "clear":
            self.log.info("Clear sound requested; paused=%s sounds=%s phone=%s volume=%s",
                          self.paused(), self.cfg.get("sounds", True), self.phone.plays_here(), volume)
        if self.paused():
            return                               # «Пауза голоса»: сигналы молчат — и на Маке, и на телефоне
        if sound == "text" and not self.cfg.get("text_sound", True):
            return                               # сигнал «фраза напечатана» выключен отдельно
        if not self.cfg.get("sounds", True) or sound not in SOUNDS:
            return
        path = self.signal_file(sound)
        if not path:
            if sound == "clear":
                self.log.info("Clear sound skipped because sound is disabled")
            return
        volume *= self.signal_volume(sound)
        if self.phone.plays_here():
            if sound in ("text", "enter", "clear") and not self.phone_sound(sound):
                if sound == "clear":
                    self.log.info("Clear sound skipped because phone sound is disabled")
                return
            if sound.startswith("buttons_") or self._signal_pick(sound).get("file") == path:
                try:
                    data, seconds = self._phone_clip(path)
                    self.phone.sound_file(data, "audio/mp4", seconds, volume)
                    return
                except (OSError, subprocess.SubprocessError) as exc:
                    self.log.warning("Could not prepare sound %s for phone: %s", sound, exc)
            fallback = COMMAND_SIGNAL_DEFAULTS.get(sound, sound)
            self.phone.signal("space" if sound == "recognize_send" else fallback, volume)
            if sound == "clear":
                self.log.info("Clear sound sent to phone")
            return
        if sound == "clear":
            self.log.info("Clear sound: file=%s exists=%s volume=%s output=%s",
                          path, Path(path).is_file(), volume, audio_out.output_key())
        self._signal.proc = audio_out.play(path, volume)
        if sound == "clear":
            self.log.info("Clear sound player started, pid=%s", self._signal.proc.pid)
        if sound == "text":
            with self._quiet_lock:
                self._quiet_signals.append(self._signal.proc)

    # ---------- «Сигналы»: свой звук и громкость для каждого действия (signal_sounds)

    def _signal_pick(self, sound):
        return (self.cfg.get("signal_sounds") or {}).get(sound) or {}

    def signal_file(self, sound):
        """Файл сигнала: выбранный в «Сигналах»; файла нет — звук по умолчанию (SOUNDS); None — «Без звука»."""
        name = self._signal_pick(sound).get("file") or ""
        if name == "off":
            return None
        if name and Path(name).is_file():
            return name
        return SOUNDS.get(sound)

    def signal_volume(self, sound):
        try:
            return max(0.0, min(1.0, float(self._signal_pick(sound).get("volume", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    def signal_library(self):
        """Звуки для выбора: свои из папки sounds/ и системные macOS — имя, путь, длина (с)."""
        out = []
        for group, folder in (("user", SIGNAL_DIR), ("system", SYSTEM_SOUNDS)):
            try:
                files = sorted(p for p in folder.iterdir() if p.suffix.lower() in SOUND_EXT)
            except OSError:
                files = []
            for p in files:
                out.append({"name": p.stem, "path": str(p), "group": group,
                            "seconds": round(audio_out.duration(p), 2)})
        return {"library": out, "defaults": {k: Path(v).stem for k, v in SOUNDS.items()}}

    def ui_log(self, kind, text):
        """Сообщение из окна (ui.html): ошибка JS или этап отрисовки. Пустое окно при старте — ищем по ним."""
        self.ui_seen = time.time()
        if kind == "ready" and getattr(self, "redraw_window", None):
            self.redraw_window()
        if kind == "error":
            self.log.warning("Window (JS): %s", text)
        else:
            self.log.info("Window: %s%s", kind, f" — {text}" if text else "")

    def signal_preview(self, sound, name, volume, where=""):
        """«▶ Прослушать»: звук из библиотеки (или по умолчанию для действия) с этой громкостью.
        where == "phone" — сигнал проигрывает телефон (блок «Сигналы на телефоне»)."""
        if where == "phone":
            try:
                volume = max(0.0, min(1.0, float(volume)))
            except (TypeError, ValueError):
                volume = 1.0
            self.phone.signal(sound, volume)
            return {"ok": True}
        known = {s["path"] for s in self.signal_library()["library"]}
        path = name if name in known else SOUNDS.get(sound)
        if not path or name == "off":
            return {"ok": False}
        try:
            volume = max(0.0, min(1.0, float(volume)))
        except (TypeError, ValueError):
            volume = 1.0
        audio_out.play(path, volume)
        return {"ok": True}

    def signal_folder(self):
        """«Открыть папку»: своя библиотека звуков в Finder."""
        SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(SIGNAL_DIR)])
        return {"ok": True}

    def phone_sound(self, name):
        """«Сигналы на телефоне» (режим tabletа): sent / text / enter / clear / beep. Выключены «Звуковые сигналы» — все нет."""
        if not self.cfg.get("sounds", True):
            return False
        return bool((self.cfg.get("phone_sounds") or {}).get(name, name != "beep"))

    _quiet_lock = threading.Lock()     # список дополняет worker, а чистит поток _watch_output — по очереди

    def _quiet_pids(self):
        """Сигналы «фраза напечатана», что ещё звучат: «пока играет любой звук» их не считает."""
        with self._quiet_lock:
            self._quiet_signals = [p for p in self._quiet_signals if p.poll() is None]
            return {p.pid for p in self._quiet_signals}

    def fire(self, commands):
        """Выполняет команды по порядку во вкладке в фокусе."""
        commands = [c for c in commands if c not in ("amigo", "jonny")]
        if not commands:
            return
        self.log.debug("Commands: %s", commands)
        self.speaker.stop()   # любая команда перебивает голос
        term = None
        for cmd in commands:
            if cmd == "stop":
                self.reader.stop()
                self.watcher.clear_held()
                self._work_stop("Stop command")
                self.phone.command(t("chat.stop"))
                self.status = t("st.voice_stopped")
                self.log.info(t("log.voice_stopped"))
            elif cmd in MODE_COMMANDS:
                self.mode_command(cmd)
                return            # после смены режима остальное не выполняем
            elif cmd in ("switch", "previous"):
                self._unsent_forget_apps()
                term = self._switch_tab(direction=-1 if cmd == "previous" else 1) or term   # следующие клавиши — уже в новую вкладку
            elif cmd == "project":
                self._unsent_forget_apps()
                term = self._switch_project() or term   # следующий проект Orca
            elif cmd.startswith("tab"):
                self._unsent_forget_apps()
                term = self._switch_tab(int(cmd[3:])) or term   # вкладка по номеру
            elif cmd == "active":
                self._unsent_forget_apps()
                term = self._return_terminal() or term          # назад в последний терминал
            elif cmd.startswith("lenta"):
                self.lenta_read(int(cmd[5:]))                   # «сборщик N»: прочитать сообщение ленты
            elif cmd == "mic_on":
                self._mic_on_cmd()
            elif cmd in ("space", "double", "enter", "clear"):
                if not term:
                    term = self._focused()
                if not term:
                    return
                self._send_key(term, cmd)

    # сколько characters напечатано с последнего Enter — столько и сотрёт «удали фразу»; счёт — свой у каждой
    # вкладки: напечатали в A, перешли в B — «удали фразу» в B не сотрёт чужого (и счёт A не обнулит)
    _unsent = None

    @staticmethod
    def _unsent_key(term):
        """Вкладка для счёта: панель Orca, терминал по tty (Джони, печать в фоне) или приложение (печать напрямую)."""
        if term.get("input_focus_id"):
            return "pin:" + term["input_focus_id"]
        if term.get("paneKey"):
            return "pane:" + str(term["paneKey"])
        if term.get("bg"):
            return "tty:" + str(term["bg"])
        return "app:" + str(term.get("handle") or term.get("title") or "")

    def _unsent_add(self, term, n):
        if self._unsent is None:
            self._unsent = {}
        key = self._unsent_key(term)
        self._unsent[key] = max(0, self._unsent.get(key, 0) + n)

    def _unsent_take(self, term):
        """Сколько стереть в этой вкладке (и счёт её — с нуля)."""
        return (self._unsent or {}).pop(self._unsent_key(term), 0)

    def _unsent_forget_apps(self):
        """Переключили вкладку внутри приложения (печать напрямую): какая теперь впереди — не знаем, счёт с нуля."""
        if self._unsent:
            for key in [k for k in list(self._unsent) if k.startswith("app:")]:
                self._unsent.pop(key, None)

    def _send_text(self, term, text, *, preserve_whitespace=False):
        """Печатает текст во вкладку: через Orca или напрямую в терминал."""
        with self.input_lock:
            self._guard_input(term)
            if term.get("via"):
                self.direct.send_text(term, text, preserve_whitespace=preserve_whitespace)
            else:
                orca_cli.send_text(term["handle"], text, preserve_whitespace=preserve_whitespace)
            self._unsent_add(term, len(text))

    AGENT_NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "grok": "Grok"}

    def _term_agent(self, term):
        """Какой агент во вкладке: из Orca или по процессам вкладки (Terminal / iTerm2)."""
        agent = term.get("agent")
        if not agent and term.get("via"):
            agent = direct.tab_agent(term)
        return agent

    def _json_backup_enabled(self):
        engine = (self.cfg.get("zipa_backup") or "").strip()
        return bool(self.cfg.get("zipa_json") and engine and engine != self.dictation() and self.dictation() != "off")

    def _stop_backup(self):
        self._backup_epoch = getattr(self, "_backup_epoch", 0) + 1
        future = getattr(self, "_backup_future", None)
        if future:
            future.cancel()
        for client in getattr(self, "_backup_clients", {}).values():
            client.stop()

    def _backup_start(self, wav):
        """Страховка (настройка `zipa_backup`): ту же запись параллельно распознаёт вторая модель — словами.
        Печать её не ждёт дольше `zipa_backup_wait_s`."""
        engine = (self.cfg.get("zipa_backup") or "").strip()
        if not self._json_backup_enabled():
            return None
        if self._backup_future is not None and not self._backup_future.done():
            self.log.debug("Fallback engine is busy with the last phrase; skipping fallback for the new phrase")
            return None
        if self._backup_pool is None:
            self._backup_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup")
        future = self._backup_pool.submit(self._backup_text, engine, wav, self._backup_epoch)
        future.engine = engine
        future.primary = self.dictation()
        future.epoch = self._backup_epoch
        self._backup_future = future
        return future

    def _backup_text(self, engine, wav, epoch=None):
        started = time.time()
        try:
            if epoch is not None and epoch != self._backup_epoch:
                return ""
            if engine == self.dictation():
                return ""
            if engine == "apple":
                text = self._backup_apple(wav)
            elif engine == "gigaam":
                text = self.gigaam.transcribe(wav)
            elif engine in ("grok", "gemini"):
                text = self._backup_clients[engine].transcribe(wav)
            elif engine == "openai":
                client = self._backup_clients["openai" if self.openai_api() else "subscription"]
                text = client.transcribe(wav)
            elif engine == "zipa":
                text = self.zipa.transcribe(wav)
            elif engine == "whisper":
                with self.whisper_lock:
                    text = self.whisper.transcribe(wav)
            else:
                raise ValueError("Unknown fallback model: " + engine)
        except Exception as e:
            self.log.warning("Fallback (%s): %s", engine, e)
            return ""
        if epoch is not None and epoch != self._backup_epoch:
            return ""
        source = getattr(wav, "diagnostic_ipa", None)
        suffix = " [zipa-source:" + json.dumps(source, ensure_ascii=False) + "]" if source else ""
        (self.log.info if text else self.log.debug)("fallback %s in %.2f s: %r%s", engine, time.time() - started, text, suffix)
        return (text or "").strip()

    def _backup_apple(self, wav):
        """Apple распознаёт только потоком: прогоняем готовую запись через start/end (в диктовке ZIPA поток свободен)."""
        apple = self._backup_clients["apple"]
        apple.ensure(self.cfg.get("language", "en"), bool(self.cfg.get("apple_punctuation", True)))
        samples = np.frombuffer(wav[44:], dtype="<i2").astype("float32") / 32767.0
        sid = apple.start(samples)
        if not sid:
            return ""
        apple.end()
        return apple.finish(sid, float(self.cfg.get("zipa_backup_wait_s", 2)))

    def _backup_result(self, backup):
        epoch = getattr(self, "_backup_epoch", 0)
        if getattr(backup, "epoch", epoch) != epoch:
            return ""
        if not hasattr(backup, "deadline"):
            backup.deadline = time.monotonic() + max(0, float(self.cfg.get("zipa_backup_wait_s", 2)))
        result = backup.result(timeout=max(0, backup.deadline - time.monotonic()))
        return result if getattr(backup, "epoch", epoch) == getattr(self, "_backup_epoch", 0) else ""

    def _backup_wake(self, segments, backup, pause_before_s=float("inf")):
        """Любая включённая страховка может подтвердить wake независимо от страховки команд.
        Текст основной модели сохраняется: между двумя транскрипциями нет точного
        выравнивания, поэтому отрезать предполагаемое слово по числу букв/фонем нельзя.
        Одиночное слово-ключ в страховке будит без печати ошибочного основного текста.
        """
        if backup is None or not self.cfg.get("keyword_mode") \
                or any(kind == "wake" for kind, _ in segments):
            return segments
        try:
            said = self._backup_result(backup)
        except Exception:
            return segments
        if not said:
            return segments
        engine = getattr(backup, "engine", self.cfg.get("zipa_backup"))
        if engine == "zipa":
            found = self._zipa_segments(said, pause_before_s)
        else:
            skip = (() if self.matcher.short(said) else self.matcher.LONG_SKIP) + self.matcher.mode_skip(said)
            found = self.matcher.segments(said, skip=skip)
        if not found or found[0][0] != "wake":
            return segments
        self.log.info("Fallback %s confirmed the wake word", engine)
        if len(found) == 1:
            segments = [(kind, value) for kind, value in segments if kind != "text"]
        return [("wake", None)] + segments

    def _backup_commands(self, segments, backup):
        """«Страховка для всех команд» (zipa_backup_commands): ZIPA не узнала ни одной команды, а текст страховки —
        короткая фраза из одних команд («Второй терминал.», «Удали фразу») — выполняем команды по нему.
        Фраза с обычными словами остаётся как есть: печатаем то, что услышала ZIPA (решение владельца 28.10)."""
        if backup is None or not self.cfg.get("zipa_backup_commands", True) or not (self.cfg.get("zipa_backup") or "").strip() \
                or any(kind != "text" for kind, _ in segments):
            return segments
        try:
            said = (self._backup_result(backup) or "").strip()
        except Exception:
            return segments
        is_ipa = getattr(backup, "engine", self.cfg.get("zipa_backup")) == "zipa"
        if not said or (not is_ipa and not self.matcher.short(said)):
            return segments
        if is_ipa:
            found = zipa_ipa.segments(said, self.matcher, self.cfg.get("zipa_templates"))
        else:
            found = self.matcher.segments(said, skip=self.matcher.mode_skip(said))
        words = lambda v: re.sub(r"[\W_]+", "", str(v or ""))
        if not any(k != "text" for k, _ in found) or any(k == "text" and words(v) for k, v in found):
            return segments
        self.log.info("Fallback commands: %r → %s", said, [k for k, _ in found])
        return found

    BACKUP_REX = ("рекс", "рэкс", "rex")   # варианты «Рекс» в тексте страховки Grok / GigaAM

    def _backup_rex(self, segments, backup):
        """Grok или GigaAM услышал «Рекс» в выбранной позиции: Enter для любой основной модели.
        Работает независимо от «Страховки для всех команд», но учитывает включённость Enter.
        Короткий ошибочный основной текст вместо одиночного Rex не печатаем."""
        backup_engine = (self.cfg.get("zipa_backup") or "").strip()
        if backup is None or backup_engine not in ("grok", "gigaam") \
                or any(kind in ("enter", "enter2") for kind, _ in segments) \
                or not self.cfg.get("commands", {}).get("enter", {}).get("enabled", True):
            return segments
        try:
            said = self._backup_result(backup)
        except Exception:
            return segments
        norm = re.sub(r"[^\w\s]+", " ", (said or "").lower()).split()
        candidates = norm if self.cfg.get("backup_rex_position", "end") == "anywhere" else norm[-1:]
        if not any(word in self.BACKUP_REX for word in candidates):
            return segments
        self.log.info("Fallback %s recognized Rex (primary model %s): %r", backup_engine, self.dictation(), said)
        phones = sum(1 for kind, t in segments if kind == "text" and t for p in t.split() if not zipa_ipa.pauses.is_pause(p))
        short_main = phones <= (7 if self.dictation() == "zipa" else 1)
        if len(norm) == 1 and short_main:
            return [(kind, t) for kind, t in segments if kind != "text"] + [("enter", None)]
        return segments + [("enter", None)]

    def _format_dictation(self, text, backup=None, *, from_phone=False):
        """Подготовить текст диктовки к печати и сообщить, нужна ли передача пробелов JSON как есть."""
        mode = self.dictation()
        ipa = mode == "zipa" and not from_phone
        if ipa:
            text = re.sub(r"([,.]+)(?=[^,.])", r"\1 ", "".join(text.split()))
        if not self.cfg.get("zipa_json"):
            return text, False
        has_backup = not from_phone and self._json_backup_enabled()
        words = ""
        if has_backup and backup is not None:
            try:
                words = self._backup_result(backup)
            except Exception:
                words = ""                               # не успела или упала — отдаём без страховки
                self.log.debug("ZIPA JSON: fallback timed out after %s s; sending phonetics alone",
                               self.cfg.get("zipa_backup_wait_s", 2))
        if self.cfg.get("zipa_json_short"):
            payload = {"text": text}
            if has_backup:
                payload["backup"] = words or ""
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), True
        engine = getattr(backup, "engine", (self.cfg.get("zipa_backup") or "").strip())
        if not ipa:
            payload = {"type": "speech_transcription", "lang": whisper_language(self.cfg), "text": text}
            if from_phone:
                payload["source"] = "phone"
            else:
                payload["engine"] = mode
            if words:
                payload["backup"] = {"engine": engine, "text": words,
                                     "script": "IPA/ZIPA" if engine == "zipa" else "text"}
            return json.dumps(payload, ensure_ascii=False), True
        payload = {"type": "speech_transcription", "script": "IPA/ZIPA", "lang": "ru",
                   "text": text,
                   "note": "Russian speech dictated via IPA transliteration, not a cipher"}
        if words:
            payload["backup"] = {"engine": engine,
                                 "lang": "ru", "text": words,
                                 "note": "same phrase recognized by a second model, in Russian words"}
        return json.dumps(payload, ensure_ascii=False), True

    def _target_label(self, term, agent=None):
        """Куда ушла фраза — для журнала: «Claude · проект», «Codex · Terminal», «Terminal»."""
        agent = agent or self._term_agent(term)
        where = term.get("worktree") or term.get("title") or "?"
        return f"{self.AGENT_NAMES.get(agent, agent)} · {where}" if agent else where

    def _input_error(self, term, e):
        """Ошибка отправки: в статус и в журнал."""
        if getattr(self, "_recognize_submit", None):
            self._recognize_submit["failed"] = True
        key = "st.direct_error" if term and term.get("via") else "st.orca_error"
        self.status = t(key, e)[:80]
        self.log.warning("%s: %s", "direct" if term and term.get("via") else "orca", e)

    input_lock = threading.RLock()
    input_epoch = 0
    input_focus = None

    def _input_pin(self):
        return self.input_focus.target if self.input_focus else None

    def input_focus_summary(self):
        if self.input_focus:
            return self.input_focus.summary()
        return {"selected": None, "selected_label": None, "available": False}

    def input_focus_state(self):
        with self.input_lock:
            return self.input_focus.state(self.route())

    def set_input_focus(self, ident):
        name = ""
        with self.input_lock:
            if self.input_focus.select(self.route(), ident):
                self.input_epoch += 1
                self.release_pending = self.typed_pending = False
                self.state["last_target"] = None
                target = self._input_pin()
                label = self.input_focus.summary()["selected_label"]
                self.status = ("Input pinned to: " + label) if target else "Input unpinned"
                if target:
                    name = self.input_focus.name_for(target)
                self.log.info("%s", self.status)
            state = self.input_focus.state(self.route())
        if name:
            self._announce_terminal(custom_name=name)
        return state

    def set_input_focus_name(self, ident, name):
        with self.input_lock:
            name = self.input_focus.set_name(self.route(), ident, name)
            state = self.input_focus.state(self.route())
        if name:
            self._announce_terminal(custom_name=name)
        return state

    def _guard_input(self, term):
        if term.get("input_epoch", self.input_epoch) != self.input_epoch:
            raise RuntimeError("The input destination changed. Repeat the phrase")
        target = self._input_pin()
        if target:
            if term.get("input_focus_id") != target["id"]:
                raise RuntimeError("Input is pinned to the selected terminal")
            current = self.input_focus.resolve()
            if not current:
                raise RuntimeError("The pinned terminal is unavailable")
            term.update(current)
        elif term.get("input_focus_id"):
            raise RuntimeError("Input is no longer pinned. Repeat the phrase")

    def _focused(self):
        with self.input_lock:
            target = self._input_pin()
            if target:
                term = self.input_focus.resolve()
                if not term:
                    self.status = "The pinned terminal is unavailable"
                    return None
                return {**term, "input_epoch": self.input_epoch, "input_focus_id": target["id"]}
            term = self._normal_focused()
            return {**term, "input_epoch": self.input_epoch} if term else None

    def _normal_focused(self):
        """Вкладка в фокусе (Orca или терминал напрямую) или None (с сообщением в статусе)."""
        route, app = self._resolve()
        if route is None:
            return None
        if app is not None and route != "orca":
            return self.direct.target(app, route)     # «умный» способ: терминал в фокусе уже выбран
        if route == "applescript" and self.cfg.get("type_in_background", False):
            front = self.direct.front_app()
            if not (front is not None and front.bundleIdentifier() in direct.TERMINALS):
                return self._bg_target(front)       # терминал не впереди — печатаем в фоне
        if route != "orca":
            try:
                term = self.direct.focused(route)
            except Exception as e:
                self.log.warning("direct: %s", e)
                term = None
            if not term:
                if not self.direct.type_to_previous and self.direct.own_front():
                    self.status = t("st.own_window")       # впереди наше окно, а прыгать в прошлое запрещено
                    self.log.info(self.status)
                    return None
                self.status = t("st.no_terminal" if route == "applescript" else "st.no_window")
                self.log.warning(self.status)
            return term

        try:
            term = orca_cli.focused_terminal()
        except Exception as e:
            self.log.warning("orca: %s", e)
            term = None
        if not term:
            self.status = t("st.no_focus")
            self.log.warning(self.status)
        return term

    def _bg_target(self, front):
        """«Печатать в фоне»: последний Terminal / iTerm2 и его вкладка — терминал вперёд не выводим.

        None — печатать некуда (статус уже выставлен).
        """
        if front is not None and self.direct.is_ours(front) and not self.cfg.get("type_to_previous", True):
            self.status = t("st.own_window")
            self.log.info(self.status)
            return None
        rec = self.last_term.snapshot()
        if not rec or rec["bundle"] not in direct.TERMINALS:
            self.status = t("st.bg_none")
            self.log.warning(self.status)
            return None
        app = self.direct.app_by_pid(rec["pid"], rec["bundle"])
        if app is None:
            self.status = t("st.active_closed", rec["name"])
            self.log.warning(self.status)
            return None
        key = rec.get("key") if rec["kind"] == "tab" else None
        if not key:
            try:
                key = direct.capture_tab(rec["bundle"])     # вкладка не запомнена — та, что впереди в терминале
            except Exception as e:
                self.log.warning("direct: %s", e)
        if not key:
            self.status = t("st.bg_none")
            self.log.warning(self.status)
            return None
        return {**self.direct.target(app, "applescript"), "bg": key}

    def bg_preview(self):
        """«Печатать в фоне» для окна: куда сейчас уйдёт печать — {ok, text}; None — галочка не действует.
        Только смотрим (статус не трогаем); пересчёт не чаще раза в 2 с."""
        if self.route() != "applescript" or not self.cfg.get("type_in_background", False):
            return None
        at, value = getattr(self, "_bg_cache", (0.0, None))
        if time.time() - at < 2:
            return value
        rec = self.last_term.snapshot()
        if not rec or rec["bundle"] not in direct.TERMINALS:
            value = {"ok": False, "text": t("bg.none")}
        elif self.direct.app_by_pid(rec["pid"], rec["bundle"]) is None:
            value = {"ok": False, "text": t("bg.closed", rec["name"])}
        else:
            key = rec.get("key") if rec["kind"] == "tab" else None
            parts = [rec["name"]]
            if not key:
                parts.append(t("bg.front_tab"))                     # вкладка не запомнена — та, что впереди
            elif str(key).startswith("/dev/"):
                parts.append(t("bg.tab", key[5:]))                  # Terminal: вкладка по tty
            else:
                parts.append(t("bg.session", str(key)[:8]))         # iTerm2: сессия по id
            agent = direct.tab_agent({"handle": rec["bundle"], "bg": key}) if str(key or "").startswith("/dev/") else None
            if agent:
                parts.append(self.AGENT_NAMES.get(agent, agent))
            nick = self.terminal_nick(key)
            if nick:
                parts.append(nick)
            value = {"ok": True, "text": " · ".join(parts)}
        self._bg_cache = (time.time(), value)
        return value

    def _announce_terminal(self, term=None, *, custom_name=""):
        if self.reading() == "off" or self.paused():
            return
        try:
            label = custom_name or self._terminal_announcement(term)
            if label:
                self.speaker.say(label[:180])
        except Exception as e:
            self.log.warning("Selected terminal name: %s", e)

    def _terminal_announcement(self, term):
        bundle = term.get("handle") if term.get("via") else None
        tty = term.get("tty") or (direct._osascript(direct.FRONT_TTY[bundle]).strip()
                                  if bundle in direct.FRONT_TTY else "")
        custom_name = self.input_focus.name_for(term, tty) if self.input_focus else ""
        if custom_name:
            return custom_name

        agent, name = term.get("agent"), term.get("title") or ""
        if term.get("via"):
            selected = dict(term, bg=tty) if tty else term
            session = next((x for x in self.orch.sessions(fresh=True) if x["tty"] == tty), None)
            agent = session["agent"] if session else self._term_agent(selected)
            name = session["nick"] if session else direct.front_tab_name(bundle)
        names = {"codex": "Codex", "claude": "Claude", "grok": "Grok", "opencode": "OpenCode", "agy": "agy", "pi": "Pi"}
        return name if self.cfg.get("terminal_announce_name_only", False) else ", ".join(
            x for x in (names.get(agent, agent or "Terminal"), name) if x)

    def _switch_tab(self, index=None, direction=1):
        """Следующая вкладка с агентом в активном проекте или вкладка №index. Возвращает её или None."""
        if self._input_pin():
            return self._focused()
        route, app = self._resolve()
        if route is None:
            self.phone.command("⇄ " + self.status)
            return None
        if route != "orca":
            if index is None:
                sessions = self.orch.sessions(fresh=True)
                if sessions:
                    return self._cycle_agent_terminal(sessions, route, app, direction)
            if direction < 0:
                self.status = "No active terminals to switch to"
                return None
            return self._switch_direct(route, index, app)

        try:
            term = orca_cli.terminal_at(index) if index else orca_cli.next_agent_terminal(direction)
            if not term:
                self.status = t("st.no_tab", index) if index else t("st.no_other_tab")
                self.log.info(self.status)
                self.phone.command("⇄ " + self.status)
                return None
            orca_cli.switch_terminal(term["handle"])
        except Exception as e:
            self.status = t("st.orca_error", e)[:80]
            self.log.warning("orca: %s", e)
            return None

        self.play("tab" if index else "previous" if direction < 0 else "switch")
        self.last_term.note(term)
        self.phone.command(t("chat.tab", index, term["title"][:40]) if index else t("chat.switch", term["title"][:40]))
        self.status = t("st.switched", term["worktree"], term["title"][:30])
        self.log.info(t("log.switched"), term["title"], term["handle"])
        self._announce_terminal(term)
        return term

    def _cycle_agent_terminal(self, sessions, route, app, direction=1):
        """Круг по живым агентским терминалам Terminal и iTerm2, включая разные окна."""
        seen = set()
        items = []
        for side in sessions:
            tty = side.get("tty")
            if tty and tty not in seen and orchestration._foreground(tty, side["pid"]):
                seen.add(tty)
                items.append(side)
        items.sort(key=lambda side: side["tty"])
        if not items:
            return None
        try:
            app = app or self.direct.front_app()
            bundle = app.bundleIdentifier() if app else ""
            current = direct._osascript(direct.FRONT_TTY[bundle]).strip() if bundle in direct.FRONT_TTY else ""
            if not current:
                current = getattr(self, "_cycle_tty", "")
            pos = next((i for i, side in enumerate(items) if side["tty"] == current), -1)
            side = items[(pos + direction) % len(items)] if pos >= 0 else items[0 if direction > 0 else -1]
            term = self.direct.select_agent_session(side, route)
            self._cycle_tty = side["tty"]
            self.last_term.note(term)
            self.play("previous" if direction < 0 else "switch")
            self._announce_terminal({**term, "tty": side["tty"]})
            self.status = "Terminal: " + side.get("nick", side["tty"])
            self.phone.command(self.status)
            self.log.info("Next terminal: %s (%s)", side.get("nick"), side["tty"])
            return term
        except Exception as exc:
            self._input_error({"via": route}, exc)
            return None

    def _switch_project(self):
        """«Переключи проект»: следующий проект Orca по кругу, фокус — в его активный терминал.
        Только для Orca: в Terminal, iTerm2 и других — статус, ничего не делаем. Возвращает терминал или None."""
        if self._input_pin():
            return self._focused()
        route, _ = self._resolve()
        if route is None:
            self.phone.command("⇄ " + self.status)
            return None
        if route != "orca":
            self.status = t("st.project_orca_only")
            self.log.info(self.status)
            self.phone.command("⇄ " + self.status)
            return None
        try:
            term = orca_cli.next_project_terminal()
            if not term:
                self.status = t("st.no_other_project")
                self.log.info(self.status)
                self.phone.command("⇄ " + self.status)
                return None
            orca_cli.switch_terminal(term["handle"])
        except Exception as e:
            self.status = t("st.orca_error", e)[:80]
            self.log.warning("orca: %s", e)
            return None

        self.play("project")
        self.last_term.note(term)
        self.phone.command(t("chat.project", term["worktree"]))
        self.status = t("st.switched_project", term["worktree"], term["title"][:30])
        self.log.info(t("log.switched_project"), term["worktree"], term["title"], term["handle"])
        self._announce_terminal(term)
        return term

    def _switch_direct(self, route, index=None, app=None):
        """Следующая вкладка терминала напрямую (Cmd+Shift+] или команда AppleScript) или вкладка №index.
        app — терминал, выбранный «умным» способом: переключаем именно его."""
        try:
            term = self.direct.target(app, route) if app is not None else None
            term = self.direct.switch_to(route, index, term) if index else self.direct.switch(route, term)
        except Exception as e:
            self._input_error({"via": route}, e)
            return None
        if not term:
            self.status = t("st.no_tab", index) if index else t("st.no_other_tab_direct")
            self.log.info(self.status)
            self.phone.command("⇄ " + self.status)
            return None
        self.play("tab" if index else "switch")
        self.last_term.note(term)
        self.phone.command(t("chat.tab", index, term["worktree"]) if index else t("chat.switch", term["worktree"]))
        key = "st.switched_window" if term.get("switched") == "window" else "st.switched_direct"
        self.status = t(key, term["worktree"])
        self.log.info(t("log.switched"), term["worktree"], route)
        self._announce_terminal(term)
        return term

    def _return_terminal(self, force=False):
        """«Активный терминал»: фокус — в последний терминал, где вы работали, и в его вкладку.

        Работает из любого приложения (и в «умном» способе тоже). Терминал закрыт или истории нет —
        говорим об этом в статусе, другой терминал не выбираем. Возвращает цель или None.
        force — возвращать, даже если впереди терминал (окно Джони)."""
        if self._input_pin():
            return self._focused()
        try:
            term, message = self.last_term.restore(force)
        except Exception as e:
            term, message = None, t("st.active_error", e)[:80]
            self.log.warning("active terminal: %s", e)
        if term is None:
            self.status = message
            self.log.info(self.status)
            self.phone.command("↩ " + self.status)
            return None
        label = f"{term['worktree']} / {term['title'][:30]}" if not term.get("via") else term["worktree"]
        self.play("active")
        self.state["last_target"] = term
        self.last_term.note(term)
        self.status = t("st.active_back", label)
        self.phone.command(t("chat.active", label))
        self.log.info(self.status)
        self._announce_terminal(term)
        return term

    def _press(self, term, key):
        """Одна клавиша (space / enter) во вкладку: через Orca или напрямую."""
        with self.input_lock:
            self._guard_input(term)
            if key == "clear":
                # Ctrl+U в Claude Code стирает только часть длинной строки — сначала столько Backspace,
                # сколько Voice напечатал с последнего Enter, потом Ctrl+U (то, что набрано руками)
                n = min(self._unsent_take(term), 4000)
                if n:
                    if term.get("via"):
                        # «Стирать сразу» (clear_all_at_once): вся фраза исчезает разом, а не по знаку (решение владельца 28.10)
                        self.direct.edit(term, n, "", burst=self.cfg.get("clear_all_at_once", True))
                    else:
                        orca_cli.send_text(term["handle"], "\x7f" * n)
            elif key == "enter":
                self._unsent_take(term)
            if term.get("via"):
                self.direct.send_key(term, key)
            elif key == "space":
                orca_cli.send_space(term["handle"])
            elif key == "clear":
                orca_cli.send_clear(term["handle"])
            else:
                orca_cli.send_enter(term["handle"])

    def _send_key(self, term, cmd):
        if cmd == "enter" and self._keyword_input():
            self.kw_awake = False                   # любой Enter усыпляет диктовку — до следующего слова-ключа
        # «агент работает»: Enter отправил то, что Voice напечатал (пустой Enter — не запрос агенту)
        typed = cmd == "enter" and (self._unsent or {}).get(self._unsent_key(term), 0) > 0
        try:
            if cmd == "double":                     # «румба»: пробел, пауза, пробел
                self._press(term, "space")
                time.sleep(DOUBLE_SPACE_PAUSE)
                self._press(term, "space")
            else:
                self._press(term, cmd)
        except Exception as e:
            self._input_error(term, e)
            return
        request = getattr(self, "_recognize_submit", None)
        if request and cmd == "enter":
            request["sent"] = True
        self.play(cmd)
        if typed:
            self._work_start(term)
        self.phone.command(t("chat." + cmd))        # в ленту чата на tabletе
        self.state["last_target"] = term
        if not term.get("jonny"):
            self.last_term.note(term)
        label = t({"space": "st.key_space", "double": "st.key_double", "clear": "st.key_clear"}.get(cmd, "st.key_enter"))
        self.status = f"{label} → {term['worktree']} / {term['title'][:30]}"
        self.log.info(t("log.sent"), cmd, term["title"], term["handle"])

    # ---------- режимы: микрофон, диктовка, озвучка

    # ---------- блок «Звук»: микрофон, озвучка, эхо (настройка sound)

    def sound(self):
        """Настройки блока «Звук»: {mic, mic_mode, speech, echo}."""
        return sound_of(self.cfg)

    def _mic_where(self):
        """Чей микрофон работает сейчас: off | mac | phone. Микрофон «телефон», а телефона нет: ушёл сам — off
        (Мак не подхватит разговор), обрыв связи — mac (только команды, см. mic_muted). «Пауза голоса» — off везде."""
        if self.paused():
            return "off"
        mic = self.sound()["mic"]
        if mic != "phone":
            return mic
        if self.phone.active():
            return "phone"
        return "mac" if self._phone_gone == "lost" and not self.speech_only() else "off"

    def _speech_where(self):
        """Где сейчас звучит озвучка: off | mac | phone. Озвучка «телефон», а телефона нет: ушёл сам — off,
        обрыв связи — mac (как прежде: озвучка остаётся). «Пауза голоса» — off везде."""
        if self.paused():
            return "off"
        speech = self.sound()["speech"]
        if speech != "phone" or self.phone.active():
            return speech
        return "mac" if self._phone_gone == "lost" else "off"

    def out_kind(self, where=None):
        """Звук идёт в наушники или в динамики: "headphones" | "speakers". where — mac | phone (по умолчанию —
        туда, где звучит озвучка; выключена — туда, где звучат сигналы)."""
        where = where or self._speech_where()
        if where == "off":
            where = "phone" if self.phone.plays_here() else "mac"
        if where == "phone":
            out = phone_output_of(self.cfg.get("phone_output"))
            if out["mode"] == "manual":
                return "headphones" if out["name"] == "headset" else "speakers"
            return "headphones" if self.cfg.get("phone_headphones") else "speakers"   # факт с телефона
        forced = self.cfg.get("mac_output_kind")    # скрытая подмена («Подробно»): auto | headphones | speakers
        if forced in ("headphones", "speakers"):
            return forced
        return audio_out.out_kind()

    def echo_now(self):
        """Защита от эха работает сейчас (echo_now): пока звучит ответ, речь — только «заткнись», текст не печатается.
        auto: звук в наушниках — защиты нет (перебивать можно); в динамиках — есть, даже если микрофон на другом
        устройстве (телефон часто лежит рядом с Маком: случай 20:50)."""
        echo = self.sound()["echo"]
        if echo != "auto":
            return echo == "on"
        return self.out_kind() != "headphones"

    def set_sound(self, patch, source="mac"):
        """Сменить часть блока «Звук» ({mic, mic_mode, speech, echo}). source — откуда: mac (окно, голос) | phone."""
        patch = sound_patch(patch)
        if source != "remote_device":
            for kind in ("mic", "speech"):
                if kind in patch:
                    self.phone.cancel_channel(kind)
        old = self.sound()
        new = {**old, **patch}
        if new == old and isinstance(self.cfg.get("sound"), dict):
            return False
        self.save_setting("sound", new)
        # прежние ключи — согласованно (их читают старые инструменты: плашка, страница 1.2.1)
        self.save_setting("mic", new["mic"] != "off" and new["mic_mode"] != "commands")
        self.save_setting("mic_mute", new["mic"] != "off" and new["mic_mode"] == "commands")
        if new["mic_mode"] != old["mic_mode"]:
            self._probe_hit = None
        if source != "phone" and self.phone.active():
            # старая страница не знает блока «Звук» — сообщаем ей прежними событиями
            if new["mic"] == "mac" and old["mic"] != "mac":
                self.phone.mic_to_mac(source)    # её микрофон выключается, на ней «Мак»
            if new["speech"] != old["speech"] and new["speech"] != "off":
                self.phone.set_reply("mac" if new["speech"] == "mac" else "text")
        if new["speech"] != old["speech"]:
            self.speaker.stop()                  # то, что звучало там, — замолкает
            self.watcher.clear_held()
        self.log.info("Sound (%s): %s", "phone" if source == "phone" else "Mac",
                      ", ".join(f"{k} {old[k]} → {new[k]}" for k in new if new[k] != old[k]))
        self._apply_sound()
        self.sync_live()
        self._mode_status()
        return True

    def _apply_sound(self):
        """Блок «Звук» → то, что работает: какой микрофон слушать, где звучать, эхо, чужой звук."""
        sound = self.sound()
        phone = getattr(self, "phone", None)
        if phone is None:
            return
        # «Пауза голоса»: звук телефона не слушаем (запоздавшие куски — тоже)
        phone.mic = "off" if self.paused() else {"phone": "tablet", "mac": "mac", "off": "off"}[sound["mic"]]
        phone.reply = "mac" if sound["speech"] == "mac" else "text"
        if getattr(self, "state", None) is not None:
            self.state["speak_answers"] = self.reading() != "off"
        lis = self.listener
        if not lis:
            return
        remote = self._mic_where() == "phone"    # микрофон «телефон», он на связи, и не «Пауза голоса»
        if lis.remote != remote:                 # источник сменился — недослушанную фразу забываем
            lis.remote = remote
            lis.reset()
        if not self._test_play and not getattr(getattr(self, "recorder", None), "id", None):
            lis.paused = self._mic_paused()
        echo = self.echo_now()
        lis.mute_on_speech = False               # пока звучит голос — микрофон слушает «заткнись» (или перебивание)
        lis.barge = not echo
        lis.mute_on_play = bool(self.cfg.get("mute_while_playing", False))   # чужой звук: как есть

    def mic_on(self):
        """Микрофон принимает диктовку."""
        return self._mic_where() != "off" and self.sound()["mic_mode"] != "commands" and not self._mic_lost()

    def speech_only(self):
        return self.sound()["mic_mode"] == "speech"

    def _keyword_input(self):
        return bool(self.cfg.get("keyword_mode")) and not self.speech_only()

    def _short_sound_stop(self):
        return bool(self.cfg.get("short_sound_stop")) and self.sound()["echo"] == "off" and not self.speech_only()

    def _mic_lost(self):
        """Микрофон «телефон», а связь с ним оборвалась: Мак временно слушает только команды."""
        return self.sound()["mic"] == "phone" and self._mic_where() == "mac"

    def mic_muted(self):
        """«Только команды»: микрофон слушает, но текст не печатает — только команды и «Амиго» / «Джони»."""
        return self._mic_where() != "off" and (self.sound()["mic_mode"] == "commands" or self._mic_lost())

    def _mic_paused(self):
        """Детектор речи стоит: микрофон «выкл» (или «телефон», а телефон ушёл сам)."""
        return self._mic_where() == "off"

    # ---------- «Пауза голоса» (решение владельца 2026-10-23, _audit_2026-10-23/55-voice-pause.md)

    def paused(self):
        """«Пауза голоса»: диктовка и озвучка выключены везде — на Маке и на телефоне. Голосом не снимается
        («Амиго», «включи диктовку», «Джони» не работают: микрофон не слушает вовсе) — только «Возобновить»
        (окно Voice, окно Goblin Remote, кнопка на телефоне). Блок «Звук» не меняется: пауза — отдельное временное
        состояние поверх него. Хранится в config.json (voice_paused) — переживает перезапуск Voice."""
        cfg = getattr(self, "cfg", None)
        return bool(cfg and cfg.get("voice_paused"))

    def set_paused(self, on, source="mac"):
        """Включить / снять «Паузу голоса». source — откуда: mac (окно Voice, окно Remote) | phone. True — сменилось."""
        on = bool(on)
        if on == self.paused():
            return False
        self.save_setting("voice_paused", on)
        self.save_setting("voice_paused_at", time.strftime("%Y-%m-%d %H:%M:%S") if on else None)
        who = "phone" if source == "phone" else "Mac"
        if on:
            self.reader.stop()                   # чтение файла — тоже голос
            self.speaker.stop()                  # текущая озвучка обрывается
            self.watcher.clear_held()            # отложенные ответы не читаются (в чат телефона ответы идут как обычно)
            self.phone.hush()                    # телефону — «стоп»: его озвучка обрывается
            self._work_stop("Voice paused")      # тики «агент работает» — тоже звук
            self.release_pending = False
            if self.listener:
                self.listener.reset()            # недослушанную фразу забываем
            while not self.phrases.empty():      # и уже дослушанные, но не разобранные
                try:
                    self.phrases.get_nowait()
                except queue.Empty:
                    break
            self.log.info("Voice paused (%s). Resume to turn microphone and speech back on", who)
        else:
            self.log.info("Voice resumed (%s). Microphone and speech follow Sound settings", who)
        self._apply_sound()
        self.sync_live()
        self._mode_status()
        return True

    WAKE = ("mic_on",)    # переводит «только команды» в «всё»

    def _wake(self, text, long=False, plain=False, pause_before_s=float("inf")):
        """Микрофон «только команды»: «включи диктовку» включает «всё» и
        выполняется целиком; в остальных фразах выполняются только команды (рекс, заткнись…), текст не печатается.
        plain — обычный текст (с телефона), а не звуки ZIPA."""
        if not text or self.speech_only():
            return
        if self.dictation() == "zipa" and not plain:
            segments = zipa_ipa.segments(text, self.matcher, self.cfg.get("zipa_templates"), pause_before_s)
        else:
            segments = self.matcher.segments(text)
        if not long and segments and segments[0][0] in self.WAKE:
            self.set_mic(True)
            self.handle_speech(text, words=plain, pause_before_s=pause_before_s)   # с телефона — слова, не звуки ZIPA
            return
        commands = self.matcher.commands(text, long) if plain else self._commands(text, long, pause_before_s)
        commands = [c for c in commands if c not in self.WAKE]
        if not commands:
            self.log.debug("Text skipped in commands-only mode: %r", text)
            return
        self.log.info("Commands only: %s → %s", text, commands)
        self.fire(commands)

    def transcribe(self, wav):
        """Фраза -> текст: в диктовке GigaAM — он (и команды тоже; сбой — запасной Whisper), иначе Whisper."""
        if self.dictation() == "openai" and not self.openai_api() and self._json_backup_enabled():
            return self._subscription_stt.transcribe(wav)
        if self.dictation() == "gemini":
            try:
                text = self.gemini.transcribe(wav)
                return text if self.dictation() == "gemini" else ""
            except Exception as e:
                self.status = "Gemini: " + str(e)
                raise
        if self.dictation() == "zipa":
            try:
                return self.zipa.transcribe(wav)
            except Exception as e:
                self.log.warning("zipa: %s; using Whisper for recognition", e)
        if self.dictation() == "gigaam":
            try:
                return self.gigaam.transcribe(wav)
            except Exception as e:
                self.log.warning("gigaam: %s; using Whisper for recognition", e)
        if self.dictation() == "grok":
            try:
                return self.grok.transcribe(wav)
            except Exception as e:
                self.log.warning("grok: %s; using Whisper for recognition", e)
        if self.stream_mode() == "openai":
            try:
                return self.oa.transcribe(wav)
            except Exception as e:
                self.log.warning("openai: %s; using Whisper for recognition", e)
        with self.whisper_lock:
            return self.whisper.transcribe(wav)

    def _safe_load(self, model):
        try:
            model.load()
        except Exception as e:
            self.log.warning("Local model: %s", e)

    def _preload_local(self):
        """Загрузить выбранные локальные модели заранее, чтобы первая фраза не ждала."""
        def run():
            if self.reading() == "gemini" or self._secondary_engine() == "gemini" or self._agent_uses_engine("gemini"):
                self.gemini_tts.warm()
            backup = self.cfg.get("zipa_backup") if self._json_backup_enabled() else None
            for need, model in ((self.dictation() == "gigaam" or backup == "gigaam", self.gigaam), (self.reading() == "silero" or self._secondary_engine() == "silero" or self._agent_uses_engine("silero"), self.silero),
                                (self.dictation() == "zipa" or backup == "zipa", self.zipa)):
                if need:
                    try:
                        model.load()
                    except Exception as e:
                        self.log.warning("Local model: %s", e)
        threading.Thread(target=run, daemon=True).start()

    def dictation(self):
        """off — только команды (Whisper), whisper — печать через Whisper, openai — через OpenAI, gigaam — через GigaAM."""
        value = self.cfg.get("dictation")
        return value if value in DICTATION else "off"

    def mic_while_speaking(self):
        """Микрофон, пока говорит голос (для плашки): commands — защита от эха, dictation — перебивать можно."""
        if not self.echo_now():
            return "dictation"
        return "off" if self.speech_only() else "commands"

    def voice_engine(self):
        """Голос озвучки (Звук → Озвучка — где, а это — чем): system | openai | silero | grok."""
        value = self.cfg.get("reading")
        return value if value in READING and value != "off" else "system"

    def _agent_uses_engine(self, engine):
        profiles = getattr(self, "terminal_voices", None)
        if profiles is None:
            return False
        return any(p.get("engine") == engine for p in profiles.sync(self.orch.sessions()).values())

    def _secondary_engine(self):
        value = self.cfg.get("secondary_reading", "")
        return value if self.cfg.get("read_scope", "all") == "all" and value in READING and value != "off" else None

    def _speaker_engine(self):
        return getattr(self.speaker, "selected_engine", None) or self.reading()

    def speech_focus(self):
        """Foreground terminal wins; otherwise use the exact retained input target.

        Do not mutate LastTerminal from the playback thread or gate a captured
        tty on NSRunningApplication lookup on that background thread.
        """
        # Фокус озвучки независим от закреплённой цели диктовки.
        # Джони получает первичный голос отдельно в _answer_is_front.
        app = self.direct.front_app()
        if app is not None and not self.direct.is_ours(app):
            bundle = app.bundleIdentifier()
            if bundle == "com.apple.Terminal":
                try:
                    tty = direct.capture_tab(bundle)
                    if tty:
                        self.log.debug("Speech: active Terminal, tty=%s", tty)
                        return {"tty": tty}
                except Exception as exc:
                    self.log.warning("Speech: failed to read active tab: %s", exc)
            elif bundle == direct.ITERM:
                try:
                    tty = direct._osascript('tell application "iTerm2" to return tty of current session of current window').strip()
                    if tty.startswith("/dev/"):
                        return {"tty": tty}
                except Exception as exc:
                    self.log.warning("Speech: failed to read active iTerm2 session: %s", exc)
            elif bundle == direct.ORCA_APP:
                try:
                    return {"orca": orca_cli.focused_terminal()}
                except Exception:
                    return {}
        rec = self.last_term.snapshot() or {}
        if rec.get("bundle") == "com.apple.Terminal" and rec.get("kind") == "tab":
            pid, tty = rec.get("pid"), rec.get("key")
            # Validate the saved process without transient AppKit lookup results.
            proc = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                                  capture_output=True, text=True, timeout=2)
            if proc.returncode == 0 and proc.stdout.strip().endswith("/Terminal.app/Contents/MacOS/Terminal"):
                script = '''tell application "Terminal"
                    repeat with w in windows
                        repeat with tabRef in tabs of w
                            if tty of tabRef is %s then return "yes"
                        end repeat
                    end repeat
                    return "no"
                end tell'''
                if tty and direct._osascript(script % direct._as_string(tty)).strip() == "yes":
                    self.log.debug("Speech: saved focus, tty=%s", tty)
                    return {"tty": tty}
            self.log.debug("Speech: saved destination unavailable, pid=%s tty=%s", pid, tty)
        elif rec.get("bundle") == direct.ITERM and rec.get("kind") == "tab":
            matches = [side for side in self.orch.speech_sessions()
                       if side.get("terminal_app") == direct.ITERM
                       and side.get("terminal_key") == rec.get("key")]
            if len(matches) == 1 and self.direct.is_running(direct.ITERM):
                return {"tty": matches[0]["tty"]}
        elif rec.get("kind") == "orca" and self.direct.is_running(direct.ORCA_APP):
            return {"orca": rec}
        self.log.debug("Speech: terminal focus missing, kind=%s bundle=%s",
                       rec.get("kind"), rec.get("bundle"))
        return {}

    def _answer_is_front(self, event):
        if event.get("paneKey"):
            return self.watcher._is_front(event, ("orca", self.speech_focus().get("orca")))
        # cwd cannot identify a tab: several agents routinely share one project.
        focus = self.speech_focus()
        if focus.get("orca"):
            return self.watcher._is_front(event, ("orca", focus["orca"]))
        if not focus.get("tty"):
            return False
        from terminal_voice import event_tty
        tty = event_tty(event, self.orch.speech_sessions())
        self.log.debug("Speech: reply tab %s, focus %s", tty or "unknown", focus["tty"])
        return tty == focus["tty"]

    def terminal_voice_state(self):
        state = self.orch.state()
        sessions = self.orch.sessions()
        profiles = self.terminal_voices.sync(sessions)
        by_id = {s.get("session_id"): profiles.get(self.terminal_voices.key(s), {}) for s in sessions}
        for row in state["sessions"]:
            row["voice"] = by_id.get(row["session_id"], {})
        return {**state, "focus": self.terminal_focus(), "input_focus": self.input_focus_summary()}

    def set_terminal_voice(self, data):
        sessions = self.orch.sessions(fresh=True)
        if data.get("session_id") and not any(s.get("session_id") == data["session_id"] and s.get("id") == data.get("id") for s in sessions):
            raise ValueError("The terminal changed. Refresh the list")
        profile = self.terminal_voices.assign(data.get("id"), data.get("profile"), sessions)
        self._preload_local()
        self.sync_live()
        return {"ok": True, "profile": profile}

    def _answer_profile(self, text):
        event = getattr(text, "event", None) or {}
        if event.get("paneKey"):
            return {}
        return self.terminal_voices.for_event(event, self.orch.speech_sessions())

    def _answer_selection(self, text):
        """Choose the manual profile or the primary/secondary voice from the speech focus."""
        event = getattr(text, "event", None) or {}
        profile = self._answer_profile(text)
        secondary = bool(event and self._secondary_engine() and not self._answer_is_front(event))
        prefix = "secondary_" if secondary else ""
        voices = {engine: self.cfg.get(prefix + key, self.cfg.get(key))
                  for engine, key in (("system", "voice"), ("silero", "silero_voice"), ("grok", "grok_voice"))}
        voices = {**{k: v for k, v in voices.items() if v},
                  **(getattr(text, "voices", None) or {}), **(profile.get("voices") or {})}
        engine = self._secondary_engine() if secondary else None
        manual = profile.get("engine")
        if manual in READING and manual != "off":
            engine = manual
        if self.reading() == "off":
            engine = None
        rate = self.cfg.get("secondary_rate", self.cfg["rate"]) if secondary else self.cfg["rate"]
        self.log.debug("Speech: tty=%s pid=%s selection=%s engine=%s",
                       event.get("tty"), event.get("pid"),
                       "manual" if manual else "secondary" if secondary else "primary", engine or self.reading())
        return {"engine": engine, "voices": voices, "rate": rate}

    def _answer_voices(self, text):
        return self._answer_selection(text)["voices"]

    def _answer_rate(self, text):
        return self._answer_selection(text)["rate"]

    def _answer_engine(self, text):
        return self._answer_selection(text)["engine"]

    def reading(self):
        """Чем читаем ответы сейчас: off — не читаем (Озвучка «выкл» или звучать негде), иначе — голос."""
        return "off" if self._speech_where() == "off" else self.voice_engine()

    def set_mic(self, on, mute=False, auto=False):
        """Прежние кнопки и голосовые команды микрофона (вкл / приглушить / выкл) → блок «Звук».
        on — включить диктовку; mute — только команды; ни то ни другое — выключен везде.
        Включаем, а он «выкл» (или телефон ушёл) — слушает Мак; телефон на связи и выбран — остаётся телефон."""
        sound = self.sound()
        if not on and not mute:
            changed = self.set_sound({"mic": "off"})
        else:
            mic = sound["mic"]
            if mic == "off" or (mic == "phone" and not self.phone.active()):
                mic = "mac"
            mode = "speech" if sound["mic_mode"] == "speech" else "all"
            changed = self.set_sound({"mic": mic, "mic_mode": mode if on else "commands"})
        if changed:
            self.log.info(t("log.mic_on" if on else "log.mic_mute" if mute else "log.mic_off"))

    def test_record(self, item_id, slot="me1"):
        """Вкладка «Тесты»: начать запись текста item_id в слот me1…me3 (пусто — остановить и сохранить).
        На время записи детектор речи не слушает никого (ни Мак, ни tablet) — сказанное не печатается."""
        if item_id:
            if slot not in voice_tests.SLOTS:
                return {"ok": False, "error": "Unknown slot"}
            self.recorder.start(item_id, slot, audio_out.chosen("in"))
            if self.listener:
                self.listener.hush = True      # глушим и микрофон Мака, и звук с tabletа
            return {"ok": True}
        seconds = self.recorder.stop()
        if self.listener:
            self.listener.hush = False
        return {"ok": True, "seconds": seconds}

    _test_run = None        # прогон тестов (процесс voice_tests_run.py)

    def test_run(self):
        """Вкладка «Тесты»: прогон отмеченных записей через отмеченные распознаватели (отдельный процесс)."""
        if self._test_run and self._test_run.poll() is None:
            return {"ok": False, "error": "A test is already running"}
        t = self.cfg.get("tests") or {}
        on = lambda group: [k for k, v in (t.get(group) or {}).items() if v]
        voices = on("voices")
        opts = {"stt": on("stt"), "kinds": on("kinds"), "repeats": int(t.get("repeats", 1) or 1),
                "sources": (list(voice_tests.SLOTS) if "me" in voices else []) + [v for v in voices if v != "me"]}
        if not opts["stt"] or not opts["sources"] or not opts["kinds"]:
            return {"ok": False, "error": "Select recognition engines, voices, and texts"}
        (voice_tests.CORPUS / "results").mkdir(parents=True, exist_ok=True)
        (voice_tests.CORPUS / "results" / "progress.json").write_text('{"done": 0, "total": 0, "now": "starting"}')
        self._test_run = subprocess.Popen([sys.executable, str(HERE / "voice_tests_run.py"), json.dumps(opts)],
                                          cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log.info("Test started: %s", opts)

        def finished(proc=self._test_run):
            # прогон закончился — отчёт открывается сам
            if proc.wait() == 0:
                self.log.info("Test finished; opening report")
                self.test_report()
            else:
                self.log.warning("Test failed (code %s). See tests/corpus/results/run.log", proc.returncode)
        threading.Thread(target=finished, daemon=True).start()
        return {"ok": True}

    def test_report(self):
        """Открыть последний отчёт прогона в браузере."""
        path = voice_tests.CORPUS / "results" / "report.html"
        if not path.is_file():
            return {"ok": False, "error": "A report is unavailable"}
        subprocess.Popen(["open", str(path)])
        return {"ok": True}

    _test_play = None       # что играет на вкладке «Тесты»: (голос, id, процесс)

    def preview_phone_sound(self, name, volume=0.3):
        """Прослушать звук сигнала на телефоне (выбор звука «идёт работа агента»): системный звук Мака
        или файл из своей библиотеки — в AAC и на телефон куском озвучки."""
        name = Path(str(name)).name
        path = next((p for p in (SYSTEM_SOUNDS / f"{name}.aiff", SIGNAL_DIR / name) if p.is_file()), None)
        if path is None:
            return {"ok": False, "error": "Audio not found"}
        if not self.phone.active():
            return {"ok": False, "error": "Your phone is disconnected from Voice"}
        data, secs = self._phone_clip(path)
        self.phone.sound_file(data, "audio/mp4", secs, max(0.05, min(1.0, float(volume))))
        return {"ok": True}

    def _phone_clip(self, path):
        """Звук Мака (aiff, wav…) для телефона: AAC m4a и длина, с. Перекодируем один раз — дальше из памяти.
        Телефон сам синтезирует только свои сигналы (remote.PHONE_SIGNALS), остальное — файлом."""
        key = str(path)
        if key not in self._clips:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "s.m4a"
                subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", key, str(out)], check=True,
                               capture_output=True, timeout=10)
                data = out.read_bytes()
            try:
                out = subprocess.run(["afinfo", key], capture_output=True, text=True, timeout=5).stdout
                secs = float(out.split("estimated duration:")[1].split()[0])
            except (IndexError, ValueError, OSError, subprocess.SubprocessError):
                secs = min(audio_out.duration(path), 3.0)    # длину не прочитали — сигнал короткий
            self._clips[key] = (data, secs)
        return self._clips[key]

    # ---------- «агент работает» (п. 31): тихий тик, пока агент думает над вашим запросом

    _clips = {}             # звук для телефона: путь -> (AAC m4a, длина с) — один раз
    _clip_busy = False      # звук для телефона перекодируется в фоне
    _clip_failed = 0.0      # когда перекодировать не вышло — повтор не раньше чем через WORK_CLIP_RETRY_S

    def _work_period(self):
        try:
            return max(5, min(120, int(self.cfg.get("work_tick_s", 15))))
        except (TypeError, ValueError):
            return 15

    def _work_start(self, term):
        """Start only the addressed terminal's clock."""
        if not self.cfg.get("work_tick", True) or self.paused():
            return
        # Capture the target before a background lookup or a focus change.
        target = dict(term)
        expected_pid = term.get("pid") if term.get("tty") and term.get("agent") else None
        tty = target.get("tty") or target.get("bg") or target.get("key")
        if not str(tty or "").startswith("/dev/"):
            try:
                if target.get("handle") == direct.ITERM:
                    key = target.get("bg") or target.get("key")
                    matches = [side for side in self.orch.speech_sessions()
                               if side.get("terminal_app") == direct.ITERM and side.get("terminal_key") == key]
                    if len(matches) == 1:
                        tty = matches[0]["tty"]
                    else:
                        tty = direct._osascript('tell application "iTerm2" to return tty of current session of current window').strip() if not key else None
                else:
                    script = direct.FRONT_TTY.get(target.get("handle"))
                    tty = direct._osascript(script).strip() if script else None
            except Exception:
                tty = None
        if str(tty or "").startswith("/dev/"):
            target["tty"] = tty

        def begin():
            if not self.cfg.get("work_tick", True) or self.paused():
                return
            pane = target.get("paneKey")
            if pane:
                event = dict(target, agent=target.get("agent") or "orca")
            else:
                matches = [side for side in self.orch.sessions() if side["tty"] == target.get("tty")
                           and (not target.get("agent") or side["agent"] == target["agent"])
                           and (expected_pid is None or side["pid"] == expected_pid)]
                if len(matches) != 1:
                    return  # Unknown identity must not share another terminal's timer.
                event = dict(matches[0])
            if not (self.cfg.get("sources") or {}).get(event.get("agent"), True):
                return
            if self.work_timers.start(event):
                self.log.debug("Agent active: %s tty=%s pid=%s; separate timer",
                               event.get("agent"), event.get("tty"), event.get("pid"))
                self._work_warm()
        threading.Thread(target=begin, daemon=True).start()

    def _work_stop(self, why):
        self.work_timers.clear()
        self.log.debug("Agent work timers stopped: %s", why)

    def _work_answer(self, ev):
        """Only this terminal's final answer/abort ends its work clock."""
        if self.work_timers.finish(ev):
            self.log.debug("Agent work ended (%s): %s tty=%s pid=%s",
                           ev.get("state"), ev.get("agent"), ev.get("tty"), ev.get("pid"))

    def _work_loop(self):
        """Dedicated clock: speech, recognition and slow OS scans cannot postpone ticks."""
        while not self._work_shutdown.is_set():
            try:
                if self.ready:
                    self._work_tick()
            except Exception:
                self.log.exception("Work sound timer failed")
            self._work_shutdown.wait(.1)

    def _work_observer(self):
        """Refresh exact terminal identities and focus outside the sound scheduler."""
        while not self._work_shutdown.is_set():
            try:
                if self.ready and self.cfg.get("work_tick", True) and not self.paused():
                    linked = self.cfg.get("sources") or {}
                    sessions = [s for s in self.orch.speech_sessions() if linked.get(s.get("agent"), True)]
                    self.work_timers.observe(sessions, WORK_MAX_S)
                    self._work_focus = self.speech_focus() if self.cfg.get("work_tick_scope", "active") == "active" else {}
            except Exception:
                self.log.exception("Work sound: refreshing terminals")
            self._work_shutdown.wait(.5)

    def _work_warm(self):
        """Звук тика для телефона — перекодировать заранее и в фоне (afconvert не держит ticker).
        Не вышло — следующая попытка не раньше чем через WORK_CLIP_RETRY_S."""
        if self._clip_busy or str(WORK_SOUND) in self._clips or time.time() - self._clip_failed < WORK_CLIP_RETRY_S:
            return
        self._clip_busy = True

        def run():
            try:
                self._phone_clip(WORK_SOUND)
            except Exception as e:
                self._clip_failed = time.time()
                self.log.warning("Work sound is not ready for phone: %s", e)
            finally:
                self._clip_busy = False
        threading.Thread(target=run, daemon=True).start()

    def _work_tick(self):
        """One clock per working terminal; at most one sound per ticker pass."""
        enabled = bool(self.cfg.get("work_tick", True))
        if enabled != self._work_enabled:
            self.work_timers.clear(reset=True)
            self._work_enabled = enabled
        if not enabled:
            return
        if self.paused():
            return self._work_stop("Voice paused")
        scope = self.cfg.get("work_tick_scope", "active")
        self.work_timers.configure(scope, self._work_period())
        if not self.cfg.get("sounds", True):
            return
        focus = self._work_focus if scope == "active" else None
        key = self.work_timers.next_due(focus)
        if key is None:
            return
        if self.phone.plays_here():
            clip = self._clips.get(str(WORK_SOUND))
            if clip is None:
                return self._work_warm()
            record = self.work_timers.claim(key)
            if record is None:
                return
            self.phone.sound_file(clip[0], "audio/mp4", clip[1], WORK_VOLUME)
        else:
            record = self.work_timers.claim(key)
            if record is None:
                return
            audio_out.play(WORK_SOUND, WORK_VOLUME)
            self._work_echo(min(audio_out.duration(WORK_SOUND), 3.0) + WORK_ECHO_MAC)
        self.log.debug("Work sound: tty=%s pid=%s mode=%s",
                       record.get("tty"), record.get("pid"), scope)

    _wake_rung = 0.0     # когда колокольчик «слушаю» прозвенел на лету (по началу фразы) — второй раз в конце не звоним
    _probe_busy = False  # проверка начала фразы ещё идёт — следующую пропускаем (ZIPA не заваливаем)
    _probe_hit = None    # (когда, сколько звуков заняло «дружище») — проверка на лету нашла слово-ключ в этой фразе

    def _probe_wake(self, ipa, segments, pause_before_s=float("inf")):
        """Колокольчик на лету прозвенел, а разбор всей фразы «дружище» не узнал (ZIPA на всей фразе слышит
        иначе: «дружися…»): фраза всё равно будит диктовку, слово-ключ отрезаем по числу звуков из проверки (01:21 28.10)."""
        hit, self._probe_hit = self._probe_hit, None
        if not hit or time.time() - hit[0] > 10 or not self.cfg.get("keyword_mode") or (segments and segments[0][0] == "wake"):
            return segments
        phones = [p for p in ipa.split() if not zipa_ipa.pauses.is_pause(p)]
        rest = " ".join(phones[hit[1]:])
        self.log.info("Wake word detected by the live check")
        return [("wake", None)] + (self._zipa_segments(rest, pause_before_s) if rest else [])

    def _zipa_segments(self, ipa, pause_before_s=float("inf")):
        """Разбор фразы ZIPA; «дружище» в начале — с точностью до звонкости первого звука: ZIPA часто слышит
        «тружище» («t r u ʒ i ʃ ɯ»), особенно на коротком куске (решение владельца 28.10: «т» и «д» — один звук)."""
        tpl = self.cfg.get("zipa_templates")
        segments = zipa_ipa.segments(ipa, self.matcher, tpl, pause_before_s)
        phones = ipa.split()
        if self.cfg.get("keyword_mode") and phones and phones[0] == "t" and (not segments or segments[0][0] != "wake"):
            voiced = zipa_ipa.segments(" ".join(["d"] + phones[1:]), self.matcher, tpl, pause_before_s)
            if voiced and voiced[0][0] == "wake":
                return voiced
        return segments

    def _wake_probe(self, wav):
        """Начало фразы (listener.probe_ms), пока ещё говорят: в режиме «по ключевому слову» «дружище» в начале —
        колокольчик сразу, а не после конца фразы (решение владельца 28.10). Сам разбор фразы — как обычно, в конце."""
        awake = self.kw_awake and time.time() - self.kw_at <= self.KW_SLEEP_S
        if not self._keyword_input() or awake or self.dictation() != "zipa" or not self.cfg.get("wake_sound", True) \
                or time.time() - self._wake_rung < 3:
            return False                         # эту фразу дальше не проверяем
        if self._probe_busy:
            return None                          # прошлая проверка ещё идёт — эту пропускаем
        self._probe_busy = True

        def run():
            try:
                ipa = self.zipa.transcribe(wav)
                if not self._keyword_input():
                    return
                segments = self._zipa_segments(ipa)
            except Exception as e:
                self.log.debug("Live wake word check: %s", e)
                return
            finally:
                self._probe_busy = False
            if segments and segments[0][0] == "wake" and time.time() - self._wake_rung >= 3:
                rest = sum(1 for k, t in segments[1:] if k == "text" and t
                           for p in t.split() if not zipa_ipa.pauses.is_pause(p))
                heard = [p for p in ipa.split() if not zipa_ipa.pauses.is_pause(p)]
                self._probe_hit = (time.time(), max(1, len(heard) - rest))
                self._wake_rung = time.time()
                self.log.info("Live wake word detected; playing bell")
                self._wake_signal()
        threading.Thread(target=run, daemon=True).start()
        return None

    def _wake_signal(self):
        """«Дружище»: тихий колокольчик «слушаю» — там же, где сигналы (телефон в «Голосе» или Мак).
        Выключатель — «wake_sound» в «Сигналах». Нашим голосом (эхо) не считается: звенит посреди фразы,
        и фраза иначе ушла бы в «речь поверх голоса» и выбросилась (01:14 28.10)."""
        if not self.cfg.get("wake_sound", True) or not self.cfg.get("sounds", True) or self.paused():
            return
        path = self.signal_file("wake")
        if not path:
            return
        volume = self.signal_volume("wake")
        if self.phone.plays_here():
            def run():                           # перекодировка (первый раз) — не в потоке разбора фраз
                try:
                    data, seconds = self._phone_clip(path)
                    self.phone.sound_file(data, "audio/mp4", seconds, volume, echo=False)
                except Exception as e:
                    self.log.warning("Listening sound: %s", e)
            threading.Thread(target=run, daemon=True).start()
        else:
            audio_out.play(path, volume)

    def _work_echo(self, seconds):
        """Тик из колонок Мака не должен попасть в распознавание: столько он считается нашим голосом
        (правила эха — как для озвучки; фразу не обрываем). В наушниках эха нет (эхо «авто»), «Эхо: выкл» — нет."""
        echo = self.sound()["echo"]
        if echo == "off" or (echo == "auto" and self.out_kind("mac") == "headphones"):
            return
        self.speaker.sound_until = max(self.speaker.sound_until, time.time() + seconds)

    def test_play(self, voice, item_id):
        """Вкладка «Тесты»: прослушать запись (повторное нажатие — остановить).
        Пока играет, детектор речи на паузе — запись не печатается в терминал."""
        prev = self._test_play
        if prev:
            prev[2].terminate()
            self._test_play = None
            if prev[:2] == (voice, item_id):
                return {"ok": True, "playing": None}
        path = voice_tests.audio_path(voice, item_id)
        if path is None or not path.is_file():
            return {"ok": False, "error": "Recording not found"}
        proc = audio_out.play(path)
        self._test_play = (voice, item_id, proc)
        if self.listener:
            self.listener.paused = True

        def done():
            proc.wait()
            if self._test_play and self._test_play[2] is proc:
                self._test_play = None
            if self.listener and not self._test_play and not self.recorder.id:
                self.listener.paused = self._mic_paused()
        threading.Thread(target=done, daemon=True).start()
        return {"ok": True, "playing": [voice, item_id]}

    def set_dictation(self, value, reason="toggle"):
        """Диктовка: off / whisper / openai. Возвращает True, если режим сменился."""
        if value not in DICTATION:
            raise ValueError(t("err.bad_mode", value))
        old = self.dictation()
        if value == old:
            return False
        self._stop_backup()
        self._subscription_stt.stop()
        self.save_setting("dictation", value)
        if self.cfg.get("zipa_backup") == value:
            self.save_setting("zipa_backup", "")
            self.save_setting("zipa_backup_commands", False)
        if self.listener:
            self.listener.dictation = value != "off"
        if value == "openai":
            self.play("mode_on")
            self.log.info(t("log.live_on"))
        elif old == "openai":
            self.play("error" if reason in ("error", "closed") else "mode_off")
            self.log.info(t("log.live_off"), t("why." + reason))
        self.log.info(t("log.dictation"), t("dict." + value))
        self.sync_live()
        self._preload_local()
        self._sync_apple()
        if reason not in ("error", "closed"):
            self._mode_status()
        return True

    def _mode_status(self):
        """Строка статуса по текущему режиму распознавания."""
        if self.paused():
            self.status = t("st.voice_paused")
        elif self.state["live"]:
            ready = bool(self.live and self.live.ws)    # связь могла быть открыта раньше ради голоса
            self.status = t("st.live_talk" if ready else "st.live_connecting")
        else:
            self.status = t("st.standard")

    def preview_state(self):
        """Local engine readiness; no synthetic speech or network probes."""
        primary = self.voice_engine()
        secondary = self._secondary_engine() or primary
        if not self._preview_lock.acquire(blocking=False):
            return {"primary": primary, "secondary": secondary, "ready": {}}
        try:
            live = self.live
            session = self.gemini_tts._session
            grok_auth = grok_voice.auth_info(self.cfg)
            blocked = ""
            if self.paused():
                blocked = "paused"
            elif not ((self._speech_where() == "phone" and self.phone.plays_here())
                      or audio_out.output_available()):
                blocked = "output"
            engines = {
                "system": bool(shutil.which("say")),
                "silero": self.silero.model is not None,
                "grok": bool(grok_auth.get("ok") or grok_auth.get("effective_mode") == "api"),
                "openai": (bool(openai_voice.api_key(self.cfg)) if self.cfg.get("openai_auth") == "api"
                           else bool(live and live.ws and not getattr(live, "_cancel", False))),
                "gemini": bool(session and session.p.poll() is None and not session.fault
                               and not session.stop_event.is_set()),
            }
            return {"primary": primary, "secondary": secondary, "blocked": blocked,
                    "ready": {engine: bool(self.ready and not blocked and ready) for engine, ready in engines.items()}}
        finally:
            self._preview_lock.release()

    def preview_voice(self, data):
        """Reject stale/connecting selections instead of reading with another engine."""
        if not self._preview_lock.acquire(blocking=False):
            return {"ok": False, "error": "Switching speech engines"}
        try:
            state = self.preview_state()
            engine = data.get("engine")
            role = data.get("role")
            if role in ("primary", "secondary") and state[role] != engine:
                return {"ok": False, "error": "The selected engine has not been applied"}
            if role == "secondary" and self.cfg.get("read_scope", "all") != "all":
                return {"ok": False, "error": "The secondary voice is off"}
            if not state["ready"].get(engine, False):
                return {"ok": False, "error": "The selected engine is not ready"}
            self.speaker.test(data.get("voice"), data.get("rate"), data.get("silero_voice"),
                              data.get("grok_voice"), engine=engine, on_mac=self._speech_where() != "phone")
            return {"ok": True}
        finally:
            self._preview_lock.release()

    def set_reading(self, value, auto=False):
        with self._preview_lock:
            return self._set_reading(value, auto)

    def _set_reading(self, value, auto=False):
        """Голос озвучки: system / openai / silero / grok; off — Озвучка «выкл» (блок «Звук»).
        Голос выбрали, а озвучка выключена — включается: на телефоне, если он в «Голосе», иначе на Маке."""
        if value not in READING:
            raise ValueError(t("err.bad_mode", value))
        self.speaker.stop()
        self.watcher.clear_held()
        if value == "off":
            self.set_sound({"speech": "off"})
        else:
            self.save_setting("reading", value)
            if self.sound()["speech"] == "off":
                self.set_sound({"speech": "phone" if self.phone.active() else "mac"})
        self._apply_sound()
        self.log.info(t("log.reading_mode"), t("read." + value))
        self.sync_live()
        self._preload_local()
        if value == "gemini" or self._secondary_engine() == "gemini" or self._agent_uses_engine("gemini"):
            self.gemini_tts.warm()
        else:
            self.gemini_tts.close()

    def mode_command(self, name):
        """Включение/выключение микрофона и озвучки. True — режим сменился."""
        group, value = MODE_COMMANDS[name]
        if name == "read_on":
            value = self.cfg.get("reading", "system")
            if value not in READING or value == "off":
                value = "system"
        if group == "mic":
            if value == "mute":                  # «приглуши микрофон»: будит «Амиго» / «Джони» / «включи диктовку»
                if self.mic_muted():
                    return False
                self.set_mic(False, mute=True)
                self.play("mic_mute")
                return True
            if self.mic_on() == value and not self.mic_muted():
                return False
            self.set_mic(value)                  # «выключи микрофон»: полностью, включить — кнопкой или с пульта
            self.play("mic_off")
            return True
        if group == "dictation":
            old = self.dictation()
            changed = self.set_dictation(value, "command")
            if changed and "openai" not in (old, value):     # для OpenAI сигнал даёт сам set_dictation
                self.play("mode_on" if value != "off" else "mode_off")
            return changed
        if self.reading() == value:
            return False
        self.set_reading(value)
        self.play("read_on" if value != "off" else "read_off")
        return True

    def sync_live(self):
        """Связь с OpenAI: нужна для диктовки OpenAI и/или для озвучки голосом OpenAI.

        Вызывается при каждой смене режима и раз в секунду (переподключение после обрыва).
        """
        # tablet (Goblin Remote) в режиме «Голос» — это тоже включённый микрофон
        listen = self.mic_on() and self.dictation() == "openai" and not self.openai_api() and not self._json_backup_enabled()
        if self.phone.active() and self.phone.mode == "text":
            listen = False      # tablet распознаёт сам («Текст»): звука для OpenAI нет — сессия слушания зависала и сбивала голос
        need = listen or (self.reading() != "off" and (self.reading() == "openai" or self._secondary_engine() == "openai" or self._agent_uses_engine("openai")) and not self.openai_api())
        with self.live_lock:
            if not self.listener:
                return
            if not need and self.live:
                self.live.stop()
                self.live = None
            elif need and not self.live and time.time() >= self.live_retry_at:
                self._open_live()

            if self.live:
                self.live.listen = listen
            self.listener.tap = self.live.feed if (self.live and listen) else None

            was = self.state["live"]
            self.state["live"] = bool(self.live) and listen
            if self.state["live"] and not was:
                self.live_started = time.time()
            if was and not self.state["live"]:
                # распознавание вернулось к Whisper: забываем накопленное, чуть-чуть не слушаем команды
                self.listener.reset()
                self.listener.ear_open()     # фразы из очереди выбрасываем — их «после сигнала» не наступит
                while not self.phrases.empty():
                    try:
                        self.phrases.get_nowait()
                    except queue.Empty:
                        break
                self.ignore_until = time.time() + 1.5

    def _open_live(self):
        """Новая сессия OpenAI (вызывается под live_lock)."""
        # колбэки знают свою сессию: старая сессия не тронет новую
        lang = whisper_language(self.cfg)       # ru / en — правило языка в инструкции; auto — без правила
        session = LiveSession(
            lambda text: self.on_live_text(text, session),
            lambda st, detail: self.on_live_state(st, detail, session),
            self.log,
            lang=None if lang == "auto" else lang,
            prompt=self.cfg.get("live_prompt", ""),
        )
        session.playback_mode = lambda: self.cfg.get("openai_playback", "stream")
        self.live = session
        session.start()

    def _reopen_live(self):
        """Новая сессия OpenAI с новой инструкцией (сменился язык)."""
        with self.live_lock:
            if not self.live:
                return
            self.live.stop()
            self.live = None
            self.live_retry_at = 0.0
        self.log.info(t("log.live_lang"), whisper_language(self.cfg))
        self.sync_live()

    def on_live_text(self, text, session):
        """Фраза от OpenAI: печатаем, команды выполняем (только когда OpenAI распознаёт)."""
        if session is not self.live or not self.state["live"]:
            return
        # «слушать после сигнала»: пока фраза печатается и звучит сигнал, в OpenAI идёт тишина
        ear = self.listener.ear_close() if self._ear_gate("openai") else None
        self._signal.proc = None
        self.heard = text.strip()
        self.log.info(t("log.live_typing"), text)
        try:
            self.handle_speech(text)
        finally:
            self._ear_after_signal(ear)

    def on_live_state(self, state, detail, session):
        self.log.debug("OpenAI Live: %s%s%s", state, f" ({detail})" if detail else "",
                       "" if session is self.live else "; skipping an earlier session")
        if session is not self.live:
            return
        if state == "live":
            self.live_fails = 0
            if self.state["live"]:
                self.status = t("st.live_talk")
            return
        if state not in ("error", "closed"):
            return

        # обрыв: сессию забываем; диктовка OpenAI -> Whisper, голос OpenAI переподключится сам
        with self.live_lock:
            if session is not self.live:
                return
            self.live = None
            self.live_fails += 1
            self.live_retry_at = time.time() + min(300, 10 * 2 ** (self.live_fails - 1))
        if state == "error":
            self.status = t("st.live_error", detail)[:80]
        if self.dictation() == "openai":
            self.set_dictation("whisper", state)
        else:
            self.sync_live()

    # ---------- пульт (кнопки в окне)

    GOBLIN_ALIVE = 6.0          # мост Goblin Remote молчит дольше — связки нет

    def goblin_link(self):
        """Сцепка с Goblin Remote: мост стучится в /api/remote/* раз в 2 с; phone — tablet в режиме «Голос»."""
        seen = getattr(self, "goblin_seen", 0.0)
        linked = time.time() - seen < self.GOBLIN_ALIVE
        return {"linked": linked, "phone": bool(linked and self.phone.active()), "running": self.goblin_running()}

    def goblin_running(self):
        """Запущено ли окно Goblin Remote (переключатель Voice | Remote); проверка не чаще раза в 3 с."""
        now = time.time()
        if now - getattr(self, "goblin_check_at", 0) > 3:
            self.goblin_check_at = now
            try:
                out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, timeout=3).stdout
                self.goblin_up = "goblin_remote.py" in out
            except Exception:
                self.goblin_up = False
        return getattr(self, "goblin_up", False)

    # ---------- Сборщик (лента сообщений, ~/Сборщик) ----------

    def lenta_url(self, path):
        return f"http://127.0.0.1:{self.cfg.get('lenta_port', 8764)}{path}"

    def lenta_read(self, n):
        """«Сборщик первое … пятое»: берём у сборщика n-е сообщение ленты и читаем его как агент «Лента»."""
        try:
            with urlopen(self.lenta_url(f"/api/item?n={n}"), timeout=5) as r:
                data = json.loads(r.read().decode() or "{}")
            text = data.get("text") or "Collector did not respond"
        except Exception as e:
            self.log.warning("Collector: %s", e)
            text = "Collector is not running"
        self.log.info("Collector: reading message %s", n)
        self.speaker.stop()
        self.watcher.push("Collector", text)

    def lenta_link(self):
        """Запущен ли сборщик; проверка не чаще раза в 5 с."""
        now = time.time()
        if now - getattr(self, "lenta_check_at", 0) > 5:
            self.lenta_check_at = now
            try:
                with urlopen(self.lenta_url("/api/status"), timeout=2) as r:
                    self.lenta_up = r.status == 200
            except Exception:
                self.lenta_up = False
        return getattr(self, "lenta_up", False)

    def show_lenta(self):
        """Вывести вперёд окно Сборщика (приложение в ~/Сборщик). Окно зовёт, только когда сборщик отвечает (lenta_link)."""
        try:
            subprocess.run(["open", str(Path.home() / "Collector" / "Collector.app")], timeout=5)
            return True
        except Exception as e:
            self.log.warning("switching to Collector: %s", e)
            return False

    # Кнопка «Запустить» на карточке локального приложения.
    LAUNCH_APPS = {
        "voice": APP_BUNDLE,   # бандл лежит в utility/, модули — в utility/core/
        "remote": Path.home() / "Desktop" / "distributive" / "goblin-remote" / "Goblin Remote Adaptive.app",
        "lenta": Path.home() / "Collector" / "Collector.app",
    }

    def launch_app(self, which):
        """Открыть приложение или сайт из меню GOBL(in)."""
        if which in ("canvas", "drag", "ai", "convert"):
            launcher = Path.home() / "Desktop" / "distributive" / "goblin-ui" / "desktop_apps.py"
            if not launcher.is_file():
                self.log.warning("Launch %s: shared script missing: %s", which, launcher)
                return {"ok": False, "error": "missing", "path": str(launcher)}
            try:
                done = subprocess.run([sys.executable, str(launcher), which], capture_output=True, text=True, timeout=15)
                result = json.loads(done.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                self.log.warning("Launch %s: %s", which, exc)
                return {"ok": False, "error": "open"}
            if not isinstance(result, dict) or type(result.get("ok")) is not bool or (done.returncode and result["ok"]):
                self.log.warning("Launch %s: invalid response from shared script, code %s", which, done.returncode)
                return {"ok": False, "error": "open"}
            return {"ok": result["ok"], **{key: result[key] for key in ("error", "path") if isinstance(result.get(key), str)}}
        app = self.LAUNCH_APPS.get(which)
        if app is None:
            return {"ok": False, "error": "unknown"}
        if not app.is_dir():
            self.log.warning("Launch %s: app missing: %s", which, app)
            return {"ok": False, "error": "missing", "path": str(app)}
        try:
            done = subprocess.run(["open", str(app)], capture_output=True, timeout=10)
        except Exception as e:
            self.log.warning("Launch %s: %s", app.name, e)
            return {"ok": False, "error": "open"}
        if done.returncode:
            self.log.warning("Launch %s: open returned %s", app.name, done.returncode)
            return {"ok": False, "error": "open"}
        self.log.info("%s: starting from the switch button", app.stem)
        return {"ok": True}

    def show_goblin(self):
        """Вывести вперёд окно Goblin Remote (macOS видит его как «Python» с goblin_remote.py в команде)."""
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=3).stdout
            pid = next((int(line.split()[0]) for line in out.splitlines() if "goblin_remote.py" in line), None)
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
            return bool(app and app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))
        except Exception as e:
            self.log.warning("Switch to Goblin Remote: %s", e)
            return False

    def phone_input_status(self):
        """Факт от телефона отдельно от сохранённого выбора микрофона."""
        if not self.phone.active() or self._mic_where() != "phone":
            return {"source": "", "warning": ""}
        diag = getattr(self.phone, "input_diag", "")
        match = re.search(r"(?:^| · )mic=([^·]+)", diag)
        source = match.group(1).strip() if match else ""
        selected = self.cfg.get("phone_input") or {}
        manual = selected.get("mode") == "manual" and selected.get("name") == "headset"
        fallback = any(text in source for text in ("Headset did not provide an input", "гарнитура не дала микрофон"))
        failed = "Гарнитура недоступна" in diag or "Headset unavailable" in diag
        warning = "fallback" if fallback else "unavailable" if failed else ""
        if manual and source.casefold() in ("phone", "телефон"):
            warning = "mismatch"
        source = source.replace("гарнитура не дала микрофон", "Headset did not provide an input").replace("телефон", "phone")
        return {"source": source, "warning": warning}

    def audio_runtime(self):
        en = self.cfg.get("ui_language") == "en"
        result = {}
        channels = self.phone.channel_state()
        for key, role, title in (("mic", "input", ("Dictation")), ("speech", "output", ("Speech playback"))):
            where = self._mic_where() if key == "mic" else self._speech_where()
            active, confirmed, host, peripheral = False, True, "", ""
            if where == "mac":
                listener = getattr(self, "listener", None)
                active = bool(listener and getattr(listener, "capture_open", False) and not listener.paused and not listener.deaf) if key == "mic" else self.speaker.busy(phone=False)
                host = getattr(self, "_device_mac_model", "Mac")
                peripheral = audio_out.in_name() if key == "mic" else audio_out.out_name()
            elif where == "phone":
                device = next((d for d in channels.get("devices", []) if d.get("id") == channels.get(role)), None)
                audio = (device or {}).get("audio_devices") or {}
                activity = audio.get("activity")
                confirmed = isinstance(activity, dict)
                active = confirmed and activity.get(role) is True
                host = audio.get("model") or (device or {}).get("name") or (("Phone"))
                peripheral = (activity or {}).get(role + "_name") or (("audio device not reported"))
            if self.paused(): active = False
            if active:
                message = title + ": " + host + " → " + str(peripheral)
                if key == "mic":
                    if self.mic_muted():
                        message += " · " + (("commands only"))
                    elif self.speech_only():
                        message += " · " + (("speech only"))
            else:
                message = title + ": " + ((("inactive")) if confirmed else (("status not confirmed")))
            result[key] = {"active": bool(active), "confirmed": confirmed, "text": message}
        return result

    def sound_info(self):
        """Блок «Звук» для окна и телефона (/api/remote/info → мост → страница): настройки, echo_now, устройства."""
        sound = self.sound()
        if not hasattr(self, "_device_mac_model"):
            self._device_mac_model = "Mac"
            try:
                hardware = json.loads(subprocess.check_output(["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"], timeout=3))
                hardware = hardware["SPHardwareDataType"][0]
                self._device_mac_model = " · ".join(str(hardware.get(key) or "") for key in ("machine_name", "machine_model")).strip(" ·") or "Mac"
            except Exception:
                pass
        # Общий список для Mac и браузера: не опрашиваем PortAudio на каждом тикe UI.
        inventory_at, audio_devices = getattr(self, "_device_audio_inventory", (0, {"in": [], "out": []}))
        if time.monotonic() - inventory_at >= 5:
            try:
                audio_devices = audio_out.devices()
            except Exception:
                audio_devices = {"in": [], "out": []}
            self._device_audio_inventory = (time.monotonic(), audio_devices)
        return {**sound, "runtime": self.audio_runtime(), "media_controls": media_controls.public_settings(self.cfg), "media_controls_devices": getattr(getattr(self, "media_keys", None), "devices", []), "media_controls_defaults": media_controls.settings({})["profiles"], "media_controls_states": getattr(getattr(self, "media_keys", None), "controller_states", {}), "media_controls_mode": getattr(getattr(self, "media_keys", None), "mode", "normal"), "media_controls_status": getattr(getattr(self, "media_keys", None), "status", "off"), "phone_input_status": self.phone_input_status(), "echo_now": self.echo_now(), "channels": self.phone.channel_state(), "device_requests": self.phone.pending_channels(),
                "android_gain_active": (self.phone.active() and self.phone.android_input
                    and self._mic_where() == "phone" and self.phone.reported_mic == "tablet"
                    and self.phone.mode == "stream" and sound["mic_mode"] != "commands"
                    and self.dictation() != "off"),
                "phone_input": self.cfg.get("phone_input") or {"mode": "auto", "name": "headset"},
                "phone_output": phone_output_of(self.cfg.get("phone_output")),
                "mac_model": self._device_mac_model, "mac_in": audio_out.in_name(), "mac_out": audio_out.out_name(),
                "mac_outputs": audio_devices["out"], "mac_inputs": audio_devices["in"],
                "audio_in": self.cfg.get("audio_in") or {"mode": "auto", "name": ""},
                "audio_out": self.cfg.get("audio_out") or {"mode": "auto", "name": ""},
                "foreign_mute": bool(self.cfg.get("mute_while_playing", False)),
                # «Пауза голоса» (55-voice-pause): внутри sound — мост и Hub пропускают sound целиком
                "paused": self.paused(), "paused_at": self.cfg.get("voice_paused_at") if self.paused() else None}

    def sound_now(self):
        """Что из блока «Звук» работает сейчас: микрофон и озвучка (off / mac / phone), куда идёт звук
        (headphones / speakers), только команды (в т. ч. из-за обрыва связи с телефоном), есть ли телефон."""
        return {"mic": self._mic_where(), "speech": self._speech_where(), "out": self.out_kind(),
                "commands": self.mic_muted(), "lost": self._mic_lost() or (self.sound()["speech"] == "phone"
                                                                          and self._speech_where() == "mac"),
                "phone": self.phone.active(), "paused": self.paused()}

    def snapshot(self):
        """Состояние для окна."""
        if self.error:
            mode = "error"
        elif not self.ready:
            mode = "starting"
        elif self.paused():
            mode = "voice_paused"                # «Пауза голоса»: микрофон и озвучка выключены до «Возобновить»
        elif self.mic_muted():
            mode = "muted"
        elif not self.mic_on():
            mode = "paused"
        elif self.state["live"]:
            mode = "live"
        elif self.speaker.busy():
            mode = "speaking"
        else:
            mode = "listening"

        target = self.state.get("last_target")
        return {
            "mode": mode,
            "goblin": self.goblin_link(),
            "lenta": self.lenta_link(),
            "held": [str(x)[:200] for x in list(self.watcher.held)],   # очередь озвучки (вкладка «Агенты»)
            "status": self.status,
            "heard": self.heard,
            "mic": self.mic_on(),
            "mic_mute": self.mic_muted(),
            "mic_missing": self.listener.missing if self.listener else None,   # выбранный микрофон не подключён
            "mic_fallback": self.listener.fallback if self.listener else None,  # …и вместо него слушаем этот
            # «Микрофон Мака» на странице 1.2.1: вкл / приглушён (только команды) / выкл
            "mic_state": ("off" if self._mic_where() != "mac" else "mute" if self.mic_muted() else "on"),
            "dictation": self.dictation(),
            "reading": self.reading(),
            "fuzzy": self.fuzzy_commands(),                                # где сейчас работает «нечёткий слух»
            "live": self.state["live"],
            "voice_ready": bool(self.live and self.live.ws),     # связь с OpenAI готова
            "preview": self.preview_state(),
            "oa_ready": self.openai_api() or bool(self.live and self.live.ws),   # голос OpenAI можно слушать
            "level": round(self.listener.level, 4) if self.listener else 0,
            "ear": self.ear_state(),                                       # слушает / фраза / глухой… (метка у полосы)
            "threshold": round(self.listener.threshold, 4) if self.listener else 0,
            "manual_mic_level": self.cfg["vad"]["min_level"],
            "auto_level": round(self.listener.auto_level, 4) if self.listener else 0,   # «Автоподстройка громкости»
            "target": f"{target['worktree']} / {target['title']}" if target else "",
            "front_term": self.front_terminal_label(),                     # «Активный терминал»: вкладка в фокусе
            "route": self.route(),
            "reader": self.reader.info(),                                  # вкладка «Чтение»: что и где читаем
            "access": direct.has_access(),
            "answer_hooks": dict(self.state.get("answer_hooks", {})),
            "input_focus": self.input_focus_summary(),
            "bg_target": self.bg_preview(),                                # «Печатать в фоне»: куда сейчас
            "phone": self.phone.active(),
            "phone_mode": self.phone.mode,                                 # stream / hold / text
            "zipa": self.zipa.state if self.dictation() == "zipa" else "",   # модель ZIPA: loading / ready / error
            "zipa_rec": self._zipa_rec[0] if self._zipa_rec and time.time() < self._zipa_rec[1] else "",
            "phone_mic": self.phone.mic if self.phone.active() else "",    # кнопка микрофона на tabletе: tablet / mac / off
            "phone_listen": self._mic_where() == "phone",                    # звук берём с tabletа
            "phone_mic_dev": getattr(self.phone, "mic_dev", "") if self.phone.active() else "",   # микрофон телефона: headset / phone
            "phone_out_dev": getattr(self.phone, "out_dev", "") if self.phone.active() else "",   # озвучка телефона: headset / speaker
            "sound_on": "phone" if self.phone.plays_here() else "mac",     # где звучат ответы и сигналы
            "phone_reply": self.phone.reply,                                 # выбор для «Голоса» с tabletа
            "voice_engine": self.voice_engine(),                             # голос озвучки (и при «Озвучка: выкл»)
            "sound": self.sound_info(),                                      # блок «Звук» — окну и телефону
            "sound_now": self.sound_now(),                                   # что из него работает сейчас
            "grok_auth": grok_voice.auth_info(self.cfg),                     # подписка или ключ API
            "openai_auth": self._openai_auth_info(),
            "gemini_auth": gemini_auth.auth_info(),
            "devices": tuple(audio_out.chosen(k) or n for k, n in zip(("in", "out"), audio_dev.names())),   # выбранные или как в macOS
            "sources": self.answer_sources(),                              # откуда берутся ответы для озвучки
            "history": {k: list(v) for k, v in self.watcher.history.items()},   # последние ответы по источникам
        }

    # ---------- плашка «слух» поверх всех окон

    BADGE_SIZE = (250, 96)       # плашка: «Диктовка ●│● Озвучка», под кружками — состояния, внизу уровень
    BADGE_SMALL = (96, 50)       # без подписей: только два кружка и уровень

    def _badge_size(self):
        return self.BADGE_SIZE if self.cfg.get("badge_label", True) else self.BADGE_SMALL

    def badge_window(self, webview_mod):
        """Окно плашки (создаётся до webview.start; скрыто, если галочка выключена)."""
        x, y = (self.cfg.get("badge_pos") or [None, None])[:2]
        if x is None:
            try:
                screen = webview_mod.screens[0]
                x, y = screen.width - self.BADGE_SIZE[0] - 24, 48      # по умолчанию — справа вверху
            except Exception:
                x, y = 40, 48
        win = webview_mod.create_window(
            "GOBL(in) Voice — Listening", self.settings.url().rstrip("/") + "/badge",
            width=self._badge_size()[0], height=self._badge_size()[1], x=x, y=y,
            frameless=True, easy_drag=True, on_top=True, transparent=True, resizable=False,
            hidden=not self.cfg.get("badge", False), focus=False)
        win.events.moved += self._badge_moved
        win.events.shown += lambda: self._badge_all_spaces(win)
        self.badge = win
        return win

    def watch_badge_clicks(self):
        """Клик по плашке не должен выводить окно Voice: после одиночного клика (и перетаскивания)
        возвращаем фокус приложению, где были; двойной клик — action «window» — показывает окно.
        Вызывать из главного потока до webview.start."""
        import AppKit

        self._window_asked = 0.0
        me = os.getpid()
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        self._prev_app = front if front and front.processIdentifier() != me else None   # до первого переключения

        def app_activated(note):
            app = note.userInfo()[AppKit.NSWorkspaceApplicationKey]
            if app.processIdentifier() != me:
                self._prev_app = app                     # куда возвращать фокус

        def voice_activated(_note):
            win = getattr(self, "badge", None)
            if not (win and win.native and self.cfg.get("badge", False)):
                return
            mouse = AppKit.NSEvent.mouseLocation()
            if not AppKit.NSPointInRect(mouse, win.native.frame()):
                self.log.debug("Badge did not activate Voice")
                return                                   # Voice стал активным не из-за плашки
            clicked = time.time()
            if time.time() - self._window_asked > 2:
                main = getattr(self, "main_window", None)
                if main and main.native and main.native.isVisible():
                    main.native.orderBack_(None)         # окно Voice не выходит вперёд от клика по плашке
            self.log.debug("Badge click: returning focus to %s",
                           self._prev_app.localizedName() if self._prev_app else "—")
            threading.Thread(target=self._badge_give_back, args=(clicked,), daemon=True).start()

        AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidActivateApplicationNotification, None, AppKit.NSOperationQueue.mainQueue(), app_activated)
        AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidBecomeActiveNotification, None, AppKit.NSOperationQueue.mainQueue(), voice_activated)

    def _badge_give_back(self, clicked):
        """Кнопку мыши отпустили и двойного клика не было — фокус обратно в прошлое приложение."""
        import AppKit
        from PyObjCTools import AppHelper
        while AppKit.NSEvent.pressedMouseButtons():          # ещё тащат плашку
            time.sleep(0.03)
        time.sleep(AppKit.NSEvent.doubleClickInterval() + 0.05)
        if self._window_asked >= clicked:
            self.log.debug("Badge double-click: keep the Voice window open")
            return                                           # был двойной клик — окно нужно
        prev = self._prev_app
        if prev is not None and not prev.isTerminated():
            AppHelper.callAfter(prev.activateWithOptions_, 0)

    def show_window(self):
        """Двойной клик по плашке: окно Voice — вперёд."""
        self._window_asked = time.time()
        win = getattr(self, "main_window", None)
        if not win:
            return
        try:
            import AppKit
            from PyObjCTools import AppHelper
            win.restore()
            win.show()
            AppHelper.callAfter(AppKit.NSApp.activateIgnoringOtherApps_, True)
        except Exception as e:
            self.log.warning("Window: %s", e)

    def _badge_all_spaces(self, win):
        """Плашка видна на всех рабочих столах и поверх полноэкранных окон.
        Окна AppKit трогаем только из главного потока (события pywebview приходят в фоновом)."""
        def apply():
            try:
                import AppKit
                win.native.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                                  | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
            except Exception as e:
                self.log.debug("Badge: %s", e)
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(apply)
        except Exception as e:
            self.log.debug("Badge: %s", e)

    def _badge_moved(self, x, y):
        """Плашку перетащили — запоминаем место (раз в полсекунды после остановки)."""
        timer = getattr(self, "_badge_timer", None)
        if timer:
            timer.cancel()
        self._badge_timer = threading.Timer(0.5, lambda: self.save_setting("badge_pos", [int(x), int(y)]))
        self._badge_timer.daemon = True
        self._badge_timer.start()

    def sync_badge(self):
        """Галочка «Плашка» — показать или спрятать окно."""
        win = getattr(self, "badge", None)
        if not win:
            return
        try:
            if self.cfg.get("badge", False):
                win.resize(*self._badge_size())     # подпись выключили — плашка меньше
                win.show()
            else:
                win.hide()
        except Exception as e:
            self.log.warning("Badge: %s", e)

    def ear_state(self):
        """Что сейчас со слухом — для метки у полосы «Микрофон»."""
        lis = self.listener
        if not self.ready or not lis:
            return "off"
        if OrcaVoice.paused(self):               # (так — и для заглушек в тестах без cfg)
            return "paused"                      # «Пауза голоса»: не слушает никто, пока не нажмут «Возобновить»
        if lis.remote:
            return "phone"                       # слушает телефон (блок «Звук» → Микрофон: телефон, он на связи)
        if lis.paused:
            return "off"
        if lis.missing and not lis.fallback:
            return "no_mic"                      # выбранный микрофон не подключён, подмена выключена — не слышит
        if lis._muted:
            return "deaf_" + (lis.mute_reason or "voice")    # играет звук / говорит голос
        if lis.deaf:
            return "wait_signal"                 # «слушать после сигнала»: ждём сигнал
        if self.state["live"]:
            return "openai"                      # звук идёт в OpenAI
        if lis.in_speech:
            return "speech"
        if self.handling or not self.phrases.empty():
            return "busy"                        # распознаёт / печатает
        return "listening"

    def action(self, name, value=None, source="mac"):
        """source — откуда: mac (окно Voice, плашка) или phone (кнопки страницы телефона, phone_control)."""
        self.log.debug("Action: %s", name)
        if name.startswith("benchmark_report."):
            reports = {"audio": "audio-recognition-60s.html", "models": "zipa-model-comparison-low.html",
                       "hybrid": "hybrid-comparison.html", "round2": "index.html"}
            filename = reports.get(name.split(".", 1)[1])
            if filename:
                batch = "2026-11-04-round2" if name == "benchmark_report.round2" else "2026-11-04-midpoint-60s"
                report = TRANSCRIPTION / "benchmarks" / batch / "reports" / filename
                if report.is_file():
                    subprocess.Popen(["open", str(report)])
                else:
                    self.log.warning("Comparison report not found: %s", report)
            return
        if name.startswith("zipa_record."):        # «🎙 Записать образец»: следующая фраза — образец команды
            key = name.split(".", 1)[1]
            rec = self._zipa_rec
            if rec and rec[0] == key and time.time() < rec[1]:   # второе нажатие — отмена записи
                self._zipa_rec = None
                self.log.info("ZIPA sample recording cancelled for %s", key)
                return
            self._zipa_rec = (key, time.time() + 20)
            self.log.info("ZIPA: say the %s command to record a sample", key)
            threading.Thread(target=lambda: self._safe_load(self.zipa), daemon=True).start()
            return
        if name.startswith("zipa_add."):
            key = name.split(".", 1)[1]
            sounds = zipa_ipa.normalize_sample(value or "")
            if key not in self.cfg.get("commands", {}) or not sounds or len(sounds) > 500:
                return
            tmpl = dict(self.cfg.get("zipa_templates") or {})
            samples = list(tmpl.get(key, []))
            tmpl[key] = (samples if sounds in samples else samples + [sounds])[-8:]
            self.save_setting("zipa_templates", tmpl)
            self.log.info("ZIPA added a text sample for %s: %s", key, sounds)
            return
        if name.startswith("zipa_edit."):          # двойной клик по образцу: поправленные звуки
            _, key, idx = name.split(".")
            sounds = zipa_ipa.normalize_sample(value or "")
            tmpl = dict(self.cfg.get("zipa_templates") or {})
            samples = list(tmpl.get(key, []))
            if sounds and idx.isdigit() and int(idx) < len(samples):
                old, samples[int(idx)] = samples[int(idx)], sounds
                tmpl[key] = samples
                self.save_setting("zipa_templates", tmpl)
                self.log.info("ZIPA updated sample %s: %s → %s", key, old, sounds)
            return
        if name.startswith("zipa_forget."):        # ✕ у образца
            _, key, idx = name.split(".")
            tmpl = dict(self.cfg.get("zipa_templates") or {})
            samples = list(tmpl.get(key, []))
            if idx.isdigit() and int(idx) < len(samples):
                gone = samples.pop(int(idx))
                tmpl[key] = samples
                self.save_setting("zipa_templates", tmpl)
                self.log.info("ZIPA removed sample %s: %s", key, gone)
            return
        if name == "window":
            self.show_window()                   # двойной клик по плашке
            return
        if name == "vad_defaults":
            # «Микрофон»: ползунки детектора речи — как в config.default.json
            defaults = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))["vad"]
            new = copy.deepcopy(self.cfg)
            for key in ("auto_level", "noise_adaptation", "min_level", "start_ratio", "silence_ms", "min_speech_ms"):
                new["vad"][key] = defaults[key]
            self.apply_config(new)
            return
        if name in ("buttons.signal.terminal", "buttons.signal.standard", "buttons.signal.extra1", "buttons.signal.extra2"):
            return self.play("buttons_" + name.rsplit(".", 1)[1])
        if name == "recognize_send":
            return self.recognize_and_send()
        group, _, value = name.partition(".")
        if group == "buttons_mode" and value in media_controls.PROFILE_NAMES:
            self.media_keys.set_mode(value)
        elif group == "device_buttons":
            if value not in ("off", "all", "mac") and not any("remote:" + d["id"] == value for d in self.phone.channel_state().get("devices", [])):
                raise ValueError("The device is disconnected")
            controls = media_controls.settings(self.cfg)
            controls["enabled"] = value != "off"
            if value != "off":
                controls["source"] = value
            self.apply_config({"media_controls": controls})
        elif group == "sound_device":
            kind, _, device = value.partition(".")
            self.phone.select_device(kind, device)
        elif group == "sound":
            # блок «Звук»: sound.mic.phone, sound.mic_mode.commands, sound.speech.mac, sound.echo.auto
            key, _, val = value.partition(".")
            if key not in SOUND_VALUES or val not in SOUND_VALUES[key]:
                raise ValueError(t("err.unknown_action", name))
            self.set_sound({key: val}, source=source)
        elif group == "mic" and value in ("on", "off", "mute"):
            # прежние кнопки (Вкл / Приглушить / Выкл): «Микрофон Мака» — на Маке; с телефона это тоже Мак
            if value == "off":
                self.set_sound({"mic": "off"}, source=source)
            else:
                mode = "speech" if self.speech_only() else "all"
                self.set_sound({"mic": "mac", "mic_mode": mode if value == "on" else "commands"}, source=source)
        elif group == "dictation":
            self.set_dictation(value)
        elif group == "reading":
            self.set_reading(value)
        elif group == "phone_reply" and value in ("text", "mac"):
            # прежнее «Где звучат ответы» (телефон / Мак) — это Озвучка в блоке «Звук»
            self.set_sound({"speech": "mac" if value == "mac" else "phone"}, source=source)
        elif group == "voice" and value in ("pause", "resume"):
            # «Пауза голоса» / «Возобновить»: окно Voice, окно Goblin Remote, кнопка ⏸ на телефоне
            self.set_paused(value == "pause", source=source)
        elif name == "clear_queue":
            self.watcher.clear_held()                # «Очистить очередь» на вкладке «Агенты»
        elif name == "hush":
            self.reader.stop()
            self.speaker.stop()
            self.watcher.clear_held()
            self._work_stop("Stop speech command")
        elif name == "read_stop":
            self.reader.stop()                       # «Стоп» на вкладке «Чтение»
        elif name == "read_pause":
            self.reader.pause()                      # «Пауза / Продолжить» на вкладке «Чтение»
        elif name in ("space", "double", "enter", "clear", "switch", "previous", "project", "active", "tab1", "tab2", "tab3", "tab4", "tab5"):
            self.fire_async([name])
        elif name == "log":
            # журнал — HTML-страницей в браузере
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines() if LOG_FILE.exists() else []
            en = self.cfg.get("ui_language", "en") == "en"
            LOG_HTML.write_text(log_html.render(lines, "GOBL(in) Voice — " + (("log")), en),
                                encoding="utf-8")
            subprocess.Popen(["open", str(LOG_HTML)])
        elif name == "log_full":
            # подробный журнал — в браузер свежими сверху: в странице только LOG_PAGE последних записей,
            # остальное (и прошлые дни из archive/) страница догружает у Voice — кнопкой «Догрузить ещё»
            lines, _ = self.log_page(0, self.LOG_PAGE)
            en = self.cfg.get("ui_language", "en") == "en"
            title = "GOBL(in) Voice — " + (("full log"))
            search = f"http://127.0.0.1:{self.cfg.get('settings_port', 8792)}/api/log/search?t={self.log_token}"
            FULL_HTML.write_text(log_html.render(lines, title, en, search, self.LOG_PAGE), encoding="utf-8")
            subprocess.Popen(["open", str(FULL_HTML)])
        elif name == "grant":
            direct.request_access()
        elif name == "amigo":
            # «Амиго» без голоса (Джони переключил терминал): режим снять, окно не возвращать — остаётся то, что впереди
            self.jonny_back = None
            threading.Thread(target=self._amigo, daemon=True).start()
        elif name == "jonny_launch":
            # «Открыть сейчас»: терминала Джони нет — открыть, есть — только привязать
            raise ValueError("Select a lead terminal in Terminals before enabling Jonny auto-start")
        else:
            raise ValueError(t("err.unknown_action", name))

    # ---------- настройки

    def _mic_input_device(self, name):
        """Restore the physical microphone profile when macOS changes its input."""
        if not name:
            return
        with CONFIG_LOCK:
            if self.cfg.get("mic_manual_active") == name:
                return
            updated = copy.deepcopy(self.cfg)
            mic_profiles.apply(self.cfg, updated, name)
            self.cfg["vad"].update(updated["vad"])
            self.cfg["mic_manual_levels"] = updated["mic_manual_levels"]
            self.cfg["mic_manual_active"] = name
            write_config(self.cfg)
            if self.listener:
                self.listener.cfg = self.cfg["vad"]
            self.log.info("Microphone: restored manual sensitivity %s: %.3f", name, updated["vad"]["min_level"])

    def apply_config(self, new):
        with self._preview_lock:
            return self._apply_config(new)

    def _apply_config(self, new):
        """Сохраняет настройки из окна и применяет их без перезапуска."""
        if new.get("answer_signal", "log") not in ("log", "hooks"):
            raise ValueError("Expected a response source of hooks or log")
        if new.get("read_scope", "all") not in ("all", "active"):
            raise ValueError("Select all replies or replies from the active terminal")
        old_audio_output = audio_out.output_key()
        # от копии до подмены self.cfg — под CONFIG_LOCK: кнопка или телефон (save_setting) в это время
        # ждут и пишут уже в новые настройки, а не в старые, которые сейчас заменятся; применение
        # (AppKit, потоки) — уже без неё: блокировка не держится дольше, чем нужно
        with CONFIG_LOCK:
            merged = copy.deepcopy(self.cfg)
            # язык окна меняется отдельно (set_language) — старая форма не должна вернуть прежний
            merged.update({k: v for k, v in new.items() if k not in ("vad", "ui_language", "ui_folds", *MODE_KEYS)})
            # ключи API окно получает маской (GET /api/config): маска или пусто — ключ не менялся
            for k, v in new.items():
                if k.endswith("api_key") and (v in (None, "", KEY_MASK) or not isinstance(v, str)):
                    if k in self.cfg:
                        merged[k] = self.cfg[k]
                    else:
                        merged.pop(k, None)
            merged["vad"] = {**self.cfg["vad"], **(new.get("vad") or {})}
            # детектор речи читает их каждый кадр: не число — поток микрофона упал бы на первой же фразе
            for key, value in merged["vad"].items():
                if key in VAD_NUMBERS and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f"vad.{key}: expected a number, got {value!r}")
            if not isinstance(merged["vad"].get("noise_adaptation", True), bool):
                raise ValueError("vad.noise_adaptation: expected a Boolean value")
            default_mic = audio_dev.names()[0]
            mic_profiles.apply(self.cfg, merged, mic_profiles.selected(merged, default_mic),
                               mic_profiles.selected(self.cfg, default_mic))
            # голос из формы — для текущего языка окна
            merged["voices"] = {**self.cfg.get("voices", {}), self.cfg.get("ui_language", "en"): merged["voice"]}
            migrate_voice_commands(merged, json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8")))

            for name, command in merged["commands"].items():
                if not isinstance(command, dict):
                    raise ValueError(t("err.bad_command", name))
                command.pop("words", None)
                build_words(command)
                if not all(isinstance(command.get(k), list) for k in WORD_KEYS):
                    raise ValueError(t("err.bad_command", name))

            if merged.get("language_mode", "ui") not in LANGUAGE_MODES:
                raise ValueError(t("err.bad_language", merged.get("language_mode")))
            merged["language"] = whisper_language(merged)

            merged["phone_output"] = phone_output_of(merged.get("phone_output"))
            if merged.get("zipa_backup") == self.dictation():
                merged["zipa_backup"] = ""
            if not merged.get("zipa_json") or not (merged.get("zipa_backup") or "").strip():
                merged["zipa_backup_commands"] = False
            merged["secondary_rate"] = max(100, min(320, int(merged.get("secondary_rate", merged["rate"]))))
            if merged.get("secondary_reading", "") not in ("", "system", "silero", "grok", "openai", "gemini"):
                raise ValueError("Unknown secondary speech engine")
            if merged.get("backup_rex_position", "end") not in ("end", "anywhere"):
                merged["backup_rex_position"] = "end"
            if merged.get("mac_output_kind", "auto") not in ("auto", "headphones", "speakers"):
                merged["mac_output_kind"] = "auto"
            # «агент работает»: да/нет и интервал 5–120 с
            try:
                merged["interim_speech_chars"] = max(20, min(20000, int(merged.get("interim_speech_chars", 120))))
            except (TypeError, ValueError, OverflowError):
                merged["interim_speech_chars"] = 120
            merged["interim_first_sentence"] = bool(merged.get("interim_first_sentence", True))
            merged["work_tick"] = bool(merged.get("work_tick", True))
            if merged.get("work_tick_scope", "active") not in ("active", "all"):
                raise ValueError("work_tick_scope: expected active or all")
            merged.setdefault("work_tick_scope", "active")
            merged["ui_debug_icons"] = bool(merged.get("ui_debug_icons", True))
            merged["speech_finish_phrase"] = bool(merged.get("speech_finish_phrase", True))
            profiles = merged.setdefault("agent_voices", {})
            if not isinstance(profiles, dict) or len(profiles) > 200:
                raise ValueError("agent_voices: expected an object with up to 200 agents")
            for agent, profile in profiles.items():
                if not isinstance(agent, str) or not agent or len(agent) > 200 or not isinstance(profile, dict):
                    raise ValueError("Invalid agent voice profile")
                if profile.get("engine", "") not in ("", "system", "silero", "grok", "openai", "gemini"):
                    raise ValueError("Invalid engine for the agent")
                voices = profile.get("voices", {})
                if not isinstance(voices, dict):
                    raise ValueError("Invalid agent voices")
                for engine, voice in voices.items():
                    if engine not in ("system", "silero", "grok", "openai") or not isinstance(voice, str) or len(voice) > 100:
                        raise ValueError("Invalid agent voice")
                    if voice and engine == "silero" and voice not in ("xenia", "kseniya", "baya", "aidar", "eugene"):
                        raise ValueError("Unknown Silero voice")
                    if voice and engine == "openai" and voice not in openai_voice.VOICES:
                        raise ValueError("Unknown OpenAI voice")
            for key, default in (("openai_playback", "stream"), ("gemini_playback", "complete")):
                if merged.get(key, default) not in ("stream", "complete"):
                    raise ValueError("Invalid playback mode: " + key)
                merged.setdefault(key, default)
            controls = merged.get("media_controls", {})
            if not isinstance(controls, dict):
                raise ValueError("media_controls: expected an object")
            presets = merged.get("button_presets", [])
            if not isinstance(presets, list):
                raise ValueError("Invalid button profile list")
            names = set()
            for preset in presets:
                name = preset.get("name") if isinstance(preset, dict) else None
                if not isinstance(name, str) or not name.strip() or len(name) > 64 or name in names:
                    raise ValueError("Give the button profile a unique name")
                names.add(name)
                modes = preset.get("profiles")
                if not isinstance(modes, dict) or set(modes) != set(media_controls.PROFILE_NAMES):
                    raise ValueError("A profile must have four button modes")
                for mapping in modes.values():
                    media_controls.validate_profile(mapping, complete=True)
            selected_profiles = controls.get("profiles", {})
            if not isinstance(selected_profiles, dict) or set(selected_profiles) - set(media_controls.PROFILE_NAMES):
                raise ValueError("Invalid button profiles")
            for mapping in selected_profiles.values():
                media_controls.validate_profile(mapping)
            profiles = controls.get("modes", {})
            if not isinstance(profiles, dict) or set(profiles) - set(media_controls.MODES):
                raise ValueError("Invalid button mode")
            for mapping in profiles.values():
                if not isinstance(mapping, dict) or set(mapping) - set(media_controls.BUTTONS) or any(action not in media_controls.ACTIONS for action in mapping.values()):
                    raise ValueError("Invalid assignment for the button mode")
            if "bindings" in controls:
                media_controls.validate_bindings(controls["bindings"])
            controls = media_controls.settings({"media_controls": controls})
            controls["enabled"] = bool(controls["enabled"])
            source = controls.get("source", "all")
            if not isinstance(source, str) or (source not in ("all", "mac") and not re.fullmatch(r"remote:[a-z0-9]{8,24}", source)):
                raise ValueError("Invalid button source")
            remote_devices = controls.get("remote_devices", {})
            if (not isinstance(remote_devices, dict) or len(remote_devices) > 64 or any(
                    not isinstance(k, str) or not re.fullmatch(r"[a-z0-9]{8,24}", k) or
                    not isinstance(v, str) or not v or len(v) > 128 for k, v in remote_devices.items())):
                raise ValueError("Invalid button source Android")
            device = controls.get("mac_device", "all")
            if not isinstance(device, str) or (device != "all" and not re.fullmatch(r"hid:[0-9a-f]{24}", device)):
                raise ValueError("Invalid media button device")
            for key in (*media_controls.BUTTONS, *(k + "_second" for k in media_controls.BUTTONS), *(k + "_again" for k in media_controls.BUTTONS)):
                if controls[key] not in media_controls.ACTIONS:
                    raise ValueError("Invalid media key action")
            if controls["enabled"] and not media_controls.settings(self.cfg)["enabled"] and sound_of(merged)["mic_mode"] != "speech":
                merged.setdefault("sound", {}).update({"mic_mode": "commands"})
                self.kw_awake = False
            merged["media_controls"] = controls
            merged["wake_sound"] = bool(merged.get("wake_sound", True))   # «дружище» — колокольчик «слушаю»
            try:
                merged["work_tick_s"] = max(5, min(120, int(merged.get("work_tick_s", 15))))
            except (TypeError, ValueError):
                merged["work_tick_s"] = 15

            if merged.get("target", "orca") not in ROUTES:
                raise ValueError(t("err.bad_target", merged.get("target")))
            route_changed = merged.get("target", "orca") != self.cfg.get("target", "orca")

            matcher = Matcher(merged)   # заодно проверка, что всё разбирается
            self._log_changes(self.cfg, merged, "settings window")
            restart_whisper = any(merged.get(k) != self.cfg.get(k) for k in WHISPER_KEYS)

            write_config(merged)

            self.cfg = merged
        if getattr(self, "orch", None):      # в тестах ядро собирают без «Оркестрации»
            self.orch.cfg = merged
        set_debug_log(merged.get("debug_log", False))
        if self.listener:
            self.listener.audio_diagnostics.enabled = bool(merged.get("audio_diagnostics", False))
        audio_out.configure(merged)
        if self.listener:
            self.listener.input_name = audio_out.chosen("in")   # сменили микрофон в окне — поток переоткроется
        self.matcher = matcher
        self.speaker.voice = merged["voice"]
        self.speaker.rate = merged["rate"]
        self.watcher.cfg = merged
        self._sources = None                     # переключили источник — показать сразу
        self.gigaam.cfg = merged
        self.silero.cfg = merged
        self.apple.cfg = merged
        self.grok.cfg = merged
        self.grok_tts.cfg = merged
        self.gemini_tts.cfg = merged
        self._stop_backup()
        self._subscription_stt.stop()
        self._subscription_stt.cfg = merged
        for client in self._backup_clients.values():
            client.cfg = merged
        self.oa.cfg = merged
        self.oa_tts.cfg = merged
        profile_changed = (merged.get("jonny") or {}).get("profile") != (self.jonny.cfg or {}).get("profile")
        self.jonny.cfg = merged.get("jonny") or {}
        if profile_changed:
            threading.Thread(target=self.jonny.apply_profile, daemon=True).start()   # сменили оформление — сразу
        self._preload_local()
        self._sync_apple()          # язык или пунктуация сменились — распознаватель перезапустится
        self.sync_badge()
        if self.listener:
            self.listener.cfg = merged["vad"]
            self.listener.echo_free = self._echo_free
            self.listener.short_sound_stop = self._short_sound_stop
            self.listener.on_barge = self._barge
            if not merged.get("listen_after_signal"):
                self.listener.ear_open()     # галочку сняли — не ждать сигнала
        self._apply_sound()                      # наушники телефона, озвучка телефона, чужой звук — сразу
        if audio_out.output_key() != old_audio_output:
            self.reader.output_changed()         # текущий фрагмент — сразу на новый выход, без отметки завершения
        self.sync_live()
        if route_changed:
            self.state["last_target"] = None     # прошлая вкладка — из другого способа отправки
            self.log.info(t("log.route"), t("route." + self.route()))
        self.log.info(t("log.settings_saved"))

        if restart_whisper:
            threading.Thread(target=self._restart_whisper, daemon=True).start()
        lang = whisper_language(merged)
        if self.live and (self.live.lang != (None if lang == "auto" else lang)
                          or self.live.prompt != merged.get("live_prompt", "")):
            threading.Thread(target=self._reopen_live, daemon=True).start()   # язык или инструкция сменились

    def _restart_whisper(self):
        self.status = t("st.whisper_restarting")
        try:
            with self.whisper_lock:
                self.whisper.restart(self.cfg)
            self.status = t("st.whisper_restarted")
        except Exception as e:
            self.status = f"Whisper: {e}"[:80]
            self.log.exception(t("log.whisper_restart"))

    def save_setting(self, key, value):
        with CONFIG_LOCK:
            self._log_changes({key: self.cfg.get(key, _MISSING)} if key in self.cfg else {}, {key: value}, "button")
            self.cfg[key] = value
            write_config(self.cfg)

    def _log_changes(self, old, new, source):
        """Подробный журнал: каждое изменение настроек — путь, было → стало."""
        for path, a, b in config_diff(old, new):
            if "api_key" in str(path):           # ключи в журнал не пишем
                a, b = ("***" if a not in (None, "", _MISSING) else a), ("***" if b else b)
            self.log.debug("Setting (%s): %s: %s → %s", source, path, short_value(a), short_value(b))

    def set_folds(self, folds):
        """Какие разделы окна раскрыты: {имя: true/false}. Запоминается между запусками."""
        if not isinstance(folds, dict):
            raise ValueError("folds")
        clean = {str(k)[:40]: bool(v) for k, v in list(folds.items())[:50]}
        self.save_setting("ui_folds", clean)

    def set_language(self, lang):
        """Язык окна, статусов, журнала и фраз голоса: ru / en. Применяется сразу.

        Заодно меняются язык распознавания Whisper, набор слов-команд (только слова этого языка)
        и голос озвучки (свой для каждого языка, настройка voices).
        """
        if lang not in i18n.LANGS:
            raise ValueError(t("err.bad_language", lang))
        self.log.debug("Window language: %s → %s", i18n.get_lang(), lang)
        i18n.set_lang(lang)
        with CONFIG_LOCK:                          # не вперемешку с сохранением настроек из окна
            self.cfg["ui_language"] = lang
            new_language = whisper_language(self.cfg)
            restart_whisper = self.cfg.get("language") != new_language
            self.cfg["language"] = new_language
            voice = self.cfg.get("voices", {}).get(lang)
            if voice:                              # озвучка — голосом этого языка
                self.cfg["voice"] = voice
                self.speaker.voice = voice
            self.save_setting("ui_language", lang)
        self.matcher = Matcher(self.cfg)
        self._sync_apple()          # диктовка «Apple» — на новый язык
        if self.live and self.live.lang != (None if new_language == "auto" else new_language):
            threading.Thread(target=self._reopen_live, daemon=True).start()
        if restart_whisper:
            if self.ready:
                threading.Thread(target=self._restart_whisper, daemon=True).start()
            else:
                self.whisper.cfg["language"] = new_language     # Whisper ещё запускается — возьмёт новый язык

        # статус на пульте переводим сразу, не дожидаясь нового события
        if self.ready and not self.error:
            self.status = t("st.live_talk" if self.state["live"] else "st.standard")
        self.log.info(t("log.language"), lang)


def build_words(command):
    """words_ru / words_en — все сочетания двух столбиков parts_ru / parts_en."""
    for words_key, parts_key in PART_KEYS.items():
        parts = command.get(parts_key)
        if isinstance(parts, list):
            parts = [[str(w) for w in col] if isinstance(col, list) else [] for col in parts[:2]]
            command[parts_key] = parts
            command[words_key] = expand_parts(parts)


def load_config():
    """Настройки из config.json; при первом запуске — копия config.default.json.

    Чего нет в старом config.json (новые настройки и команды) — берём из настроек по умолчанию.
    """
    defaults = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if not CONFIG_FILE.exists():
        shutil.copy(DEFAULT_CONFIG, CONFIG_FILE)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("secondary_rate", cfg.get("rate", defaults["rate"]))
    for key in ("voice", "silero_voice", "grok_voice"):
        cfg.setdefault("secondary_" + key, cfg.get(key, defaults.get(key, "")))
    migrated_commands = migrate_voice_commands(cfg, defaults)

    # старые тумблеры -> диктовка и озвучка (до подстановки настроек по умолчанию)
    if "dictation" not in cfg and "voice_typing" in cfg:
        cfg["dictation"] = "whisper" if cfg["voice_typing"] else "off"
    if "reading" not in cfg and "speak_answers" in cfg:
        cfg["reading"] = ("openai" if cfg.get("tts") == "openai" else "system") if cfg["speak_answers"] else "off"
    obsolete = ("voice_typing", "speak_answers", "tts", "zipa_json_codex", "zipa_json_grok")
    migrated_modes = migrated_commands or any(k in cfg for k in obsolete)
    if cfg.get("read_scope") == "leader":
        cfg["read_scope"] = "all"
        migrated_modes = True
    # GOBLIN.md: с 05.11.2026 порт Сборщика — 8764; 8765 занят другим проектом.
    if cfg.get("lenta_port") in (8765, "8765"):
        cfg["lenta_port"] = 8764
        migrated_modes = True
    for old in obsolete:
        cfg.pop(old, None)
    # тумблер «выключать микрофон, пока говорит голос» -> выбор из трёх режимов
    if "mute_while_speaking" in cfg:
        cfg.setdefault("mic_while_speaking", "off" if cfg.pop("mute_while_speaking") else "commands")
        migrated_modes = True

    # блок «Звук» (2026-10-23): микрофон, озвучка, эхо — одной настройкой sound; копия до перевода — config.json.bak-sound
    if not isinstance(cfg.get("sound"), dict):
        backup = CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak-sound")
        if not backup.exists():
            shutil.copy2(CONFIG_FILE, backup)
            os.chmod(backup, 0o600)                  # в копии — ключи API
        migrate_sound(cfg)
        migrated_modes = True

    # набор команд 2026-10-24 (migrate_commands): копия до правки — config.json.bak-cmds
    if migrate_commands(copy.deepcopy(cfg)):
        backup = CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak-cmds")
        if not backup.exists():
            shutil.copy2(CONFIG_FILE, backup)
            os.chmod(backup, 0o600)                  # в копии — ключи API
        migrate_commands(cfg)
        migrated_modes = True

    signal_sounds = cfg.get("signal_sounds")
    if isinstance(signal_sounds, dict):
        for action, previous in COMMAND_SIGNAL_DEFAULTS.items():
            if action not in signal_sounds and isinstance(signal_sounds.get(previous), dict):
                signal_sounds[action] = copy.deepcopy(signal_sounds[previous])
                migrated_modes = True

    for key, value in defaults.items():
        cfg.setdefault(key, value)
    for section in ("commands", "vad", "tests", "signal_sounds", "jonny"):
        for key, value in defaults[section].items():
            cfg[section].setdefault(key, value)
    for group in ("stt", "voices", "kinds"):          # «Тесты»: новые распознаватели / голоса / виды текстов
        for key, value in defaults["tests"][group].items():
            cfg["tests"][group].setdefault(key, value)

    # старый формат команд: один список words -> words_ru / words_en (по алфавиту слова)
    migrated = False
    for command in cfg["commands"].values():
        if "words" in command:
            words = command.pop("words")
            command.setdefault("words_en", [w for w in words if word_lang(w) == "en"])
            migrated = True
    # слова -> два столбика: старые списки целиком уходят в первый столбик (ничего не теряется)
    for command in cfg["commands"].values():
        for words_key, parts_key in PART_KEYS.items():
            if not isinstance(command.get(parts_key), list):
                command[parts_key] = [list(command.get(words_key) or []), []]
                migrated = True
        build_words(command)
    templates = cfg.get("zipa_templates") or {}
    normalized = {key: [zipa_ipa.normalize_sample(x) for x in samples]
                  for key, samples in templates.items()}
    if normalized != templates:
        cfg["zipa_templates"] = normalized
        migrated = True
    if cfg.get("zipa_backup") and cfg.get("zipa_backup") == cfg.get("dictation"):
        cfg["zipa_backup"] = ""
        migrated = True
    if cfg.get("zipa_backup_commands") and (
            not cfg.get("zipa_json") or not (cfg.get("zipa_backup") or "").strip()):
        cfg["zipa_backup_commands"] = False
        migrated = True
    if migrated:
        write_config(cfg)

    # голос озвучки — свой для каждого языка окна
    if cfg["ui_language"] != "en":
        cfg["ui_language"] = "en"
        migrated_modes = True
    cfg["voice"] = cfg["voices"].get("en") or cfg["voice"]

    # Whisper слушает язык окна (EN / RU) или определяет язык сам
    if cfg.get("language_mode") not in LANGUAGE_MODES:
        cfg["language_mode"] = "ui"
    if cfg.get("language") != whisper_language(cfg) or migrated_modes:
        cfg["language"] = whisper_language(cfg)
        write_config(cfg)
    return cfg


def set_app_name(name):
    """Имя в меню macOS вместо «Python» и логотип Goblin в Dock вместо значка python."""
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = name
        info["CFBundleDisplayName"] = name
    except Exception:
        pass
    set_dock_icon()


def set_dock_icon():
    """Значок в Dock (icon.png, tools/make_icon.py). macOS сбрасывает его, когда окно запускается, —
    поэтому зовём ещё раз из webview.start() в главном потоке."""
    try:
        from AppKit import NSApplication, NSImage
        path = ROOT / "icon.png"
        icon = NSImage.alloc().initWithContentsOfFile_(str(path if path.exists() else UI_DIR / "logo.svg"))
        if icon:
            NSApplication.sharedApplication().setApplicationIconImage_(icon)
    except Exception:
        pass


def set_dock_icon_later():
    """Из потока webview.start(): поставить значок в главном потоке, когда окно уже открыто."""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(set_dock_icon)
    except Exception:
        pass


def main():
    log = setup_log()
    cfg = load_config()
    i18n.set_lang(cfg["ui_language"])   # язык — до первых записей в журнал
    set_debug_log(cfg.get("debug_log", False))
    kill_previous(log)

    core = OrcaVoice(cfg, log)
    core.start()

    # закрыли окно или прислали SIGTERM — гасим всё
    def on_term(*_):
        core.shutdown()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)

    set_app_name("GOBL(in) Voice")
    window = webview.create_window("GOBL(in) Voice", core.settings.url(),
                                   width=960, height=920, min_size=(560, 600), hidden=True)   # ширина — как у окна Goblin Remote
    from window_render import install as install_window_render
    core.redraw_window = install_window_render(window, log)
    window.events.closed += on_term   # крестик — выходим целиком
    core.main_window = window
    core.badge_window(webview)        # плашка «слух» поверх всех окон (скрыта, если галочка выключена)
    try:
        core.watch_badge_clicks()     # клик по плашке не выводит окно Voice, двойной — выводит
    except Exception as e:
        log.warning("Badge: %s", e)
    def watch_window():
        """Окно открыто, но страница не отозвалась — пишем в журнал (плохо отрисованное окно, см. plan.md № 26)."""
        time.sleep(12)
        if not core.ui_seen:
            log.warning("Window did not respond after 12 seconds. It may be blank. "
                        "Open http://127.0.0.1:8792/ in a browser and check the console")
    threading.Thread(target=watch_window, daemon=True).start()
    webview.start(set_dock_icon_later)   # ждёт, пока окно открыто; значок — когда оно уже запустилось

    core.shutdown()
    os._exit(0)              # добиваем фоновые потоки


if __name__ == "__main__":
    main()
