"""Озвучка ответов ИИ: следим за агентами в Orca и читаем ответ голосом macOS (say)."""

import datetime
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import audio_out


class Voiced(str):
    """Текст озвучки со своим голосом: {"silero": …, "grok": …} (ответы Джони). Обычная строка — голос по умолчанию."""
    voices = None

    @classmethod
    def make(cls, text, voices, event=None):
        out = cls(text)
        out.voices = voices
        out.event = event
        return out
import orca_cli
from answer_hooks import HOOK_AGENTS
from i18n import t


PHRASE_WORD_MARGIN = 4      # завершение фразы: отклонение от лимита не больше четырёх слов
INTERIM_MAX = 120            # лимит промежуточной реплики между вызовами инструментов
FULL_MAX = 2000              # «читать целиком» (свой агент, full): только защита от огромного текста


def _last_sentence(text):
    """Последнее предложение текста (после последней «. ! ? …»)."""
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p.strip()]
    return parts[-1].strip() if len(parts) > 1 else ""


def interim_for_speech(text, limit=INTERIM_MAX, first_sentence=True):
    """Подготовить промежуточную реплику, соблюдая лимит и настройку предложения."""
    try:
        limit = max(20, min(20000, int(limit)))
    except (TypeError, ValueError, OverflowError):
        limit = INTERIM_MAX
    text = clean_for_speech(text, 10 ** 6)
    if first_sentence:
        text = re.split(r"(?<=[.!?…])\s+", text, maxsplit=1)[0].strip()
    if len(text) > limit:
        end = _speech_end(text, limit - 1, False) or limit - 1
        text = text[:end].rstrip(" ,;:—-")
        if text and text[-1] not in ".!?…":
            text += "."
    return text


def _speech_end(text, limit, finish_phrase):
    """Граница слова в лимите; по галочке — ближайшая фраза в пределах четырёх слов."""
    words = list(re.finditer(r"\S+", text))
    before = [word for word in words if word.end() <= limit]
    strict_end = before[-1].end() if before else 0
    if not finish_phrase:
        return strict_end
    after = [word for word in words if word.end() > limit]
    lower = before[-PHRASE_WORD_MARGIN - 1].end() if len(before) > PHRASE_WORD_MARGIN else 0
    upper = after[min(PHRASE_WORD_MARGIN, len(after)) - 1].end() if after else len(text)
    ends = [m.end() for m in re.finditer(r'''[.!?…;,][»”"')\]]*(?=\s|$)''', text)
            if lower <= m.end() <= upper]
    if len(text) <= upper:
        ends.append(len(text))
    return min(ends, key=lambda end: (abs(end - limit), end)) if ends else strict_end


def clean_for_speech(text, limit, say_cut=True, finish_phrase=False):
    """Markdown -> обычный текст, который приятно слушать.
    Длинный ответ — по границе слова; finish_phrase допускает до четырёх слов до конца фразы.
    say_cut=False — без вздоха «…ох» (пропуск слышен только как пауза «…»)."""
    text = re.sub(r"```.*?```", t("say.code"), text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)     # [текст](ссылка) -> текст
    text = re.sub(r"https?://\S+", t("say.link"), text)
    text = re.sub(r"`([^`]*)`", r"\1", text)                  # `код` -> код
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)      # маркеры списков
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)         # заголовки
    text = re.sub(r"[*_>|#~]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        end = _speech_end(text, limit, finish_phrase)
        if end < len(text):
            text = text[:end].rstrip()
            if say_cut:
                text += " " + t("say.cut").lstrip()
            elif text and text[-1] not in ".!?…;,»\"”')]":
                text += "."
    return text


class Speaker:
    """Один голос за раз: новая фраза прерывает старую.

    Голос — системный (say) или OpenAI: remote() возвращает live-сессию,
    если выбрана озвучка OpenAI и режим OpenAI включён, иначе None.
    """

    sound_until = 0.0      # свой короткий звук из колонок Мака (тик «агент работает»): до этого времени — как голос

    def __init__(self, voice, rate, log):
        self.voice = voice
        self.rate = rate
        self.log = log
        self.remote = lambda: None
        self.phone = None          # Remote: планшет в режиме «Голос» — говорим там, а не на Маке
        self.local = None          # local_voice.Silero: локальный русский голос
        self.grok = None           # grok_voice.GrokTTS: голос Grok (xAI)
        self.cloud = lambda: None  # озвучка через HTTP, которая сейчас выбрана (Grok / OpenAI по ключу) или None
        self.phone_audio = None    # (text) -> True: озвучка Silero / Grok / OpenAI звуком на телефон (режим «Поток»)
        self.phone_audio_stop = None
        self.engine = lambda: "system"   # «silero» — читать голосом Silero (русский текст)
        self.muted = lambda: False       # «Пауза голоса» (OrcaVoice.paused): не читаем ничего — ни на Маке, ни на телефоне
        self.selected_engine = None
        self.selected_rate = None
        self.rate_for = lambda text: self.rate
        self.selected_rate = None
        self.voices_for = lambda text: {}
        self.selected_voices = {}
        self.selection_for = None
        self.engine_for = lambda text: None
        self._proc = None
        self._lock = threading.Lock()
        self._last_busy = 0.0
        self.text = ""             # что читается сейчас — по нему микрофон узнаёт эхо озвучки
        # когда голос звучал в последний раз — следим сами: текст с планшета приходит без звука,
        # и busy_or_recent() никто не вызывает, пока голос говорит
        threading.Thread(target=self._track, daemon=True).start()

    def _track(self):
        speaking = False
        while True:
            try:
                now = self.busy()
                if now:
                    self._last_busy = time.time()
                if now != speaking:
                    speaking = now
                    if self.phone is not None:
                        self.phone.mac_speaking(now)     # планшет слушает, а звук на Маке — пусть не слышит эхо
            except Exception:
                pass
            time.sleep(0.1)

    def quiet_for(self):
        """Сколько секунд голос молчит (0 — говорит сейчас)."""
        return 0.0 if self.busy() else time.time() - self._last_busy

    def _phone(self):
        return self.phone if self.phone is not None and self.phone.plays_here() else None   # «Звук на Маке» — здесь

    @property
    def playback_rate(self):
        return getattr(self, "selected_rate", None) or self.rate

    def say(self, text):
        if self.muted():
            self.log.debug("Speech paused, skipped %d characters", len(text))
            return
        self.text = text
        phone = self._phone()
        if phone:
            with self._lock:
                self._kill()
                choice = self.selection_for(text) if self.selection_for else None
                if choice is not None:
                    self.selected_engine = choice["engine"]
                    self.selected_rate = choice["rate"]
                    self.selected_voices = choice["voices"]
                else:
                    self.selected_engine = self.engine_for(text)
                    self.selected_rate = getattr(self, "rate_for", lambda text: self.rate)(text)
                    self.selected_voices = {**(getattr(text, "voices", None) or {}), **self.voices_for(text)}
            if self.phone_audio and self.phone_audio(text):
                return                    # Silero / Grok — звуком с Мака
            phone.speak(text, self.selected_voices.get("system") or self.voice, self.playback_rate)
            self.log.debug("Speech on tablet, %d characters", len(text))
            return
        if not audio_out.output_available():
            with self._lock:
                self._kill()
            self.log.warning("Speech: waiting for output “%s” to connect", audio_out.chosen("out"))
            return
        with self._lock:
            self._kill()
            choice = self.selection_for(text) if self.selection_for else None
            if choice is not None:
                self.selected_engine = choice["engine"]
                self.selected_rate = choice["rate"]
                self.selected_voices = choice["voices"]
            else:
                self.selected_engine = self.engine_for(text)
                self.selected_rate = getattr(self, "rate_for", lambda text: self.rate)(text)
                self.selected_voices = {**(getattr(text, "voices", None) or {}), **self.voices_for(text)}
            remote = self.remote()
            if remote and remote.speak(text):
                self.log.debug("OpenAI speech, %d characters", len(text))
                return                    # читает голос OpenAI
            own = self.selected_voices
            if self._silero(text, voice=own.get("silero")):
                self.log.debug("Silero speech, %d characters", len(text))
                return                    # читает Silero
            if self._grok(text, voice=own.get(self.engine())):
                self.log.debug("Grok speech, %d characters", len(text))
                return                    # читает Grok
            self.log.debug("macOS speech %s, speed %s, %d characters", self.voice, self.playback_rate, len(text))
            self._proc = subprocess.Popen(
                ["say", "-v", own.get("system") or self.voice, "-r", str(self.playback_rate), *audio_out.say_args(), "--", text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def stop(self):
        if self.busy():
            self.log.debug("Speech stopped")
        if self.phone_audio_stop:
            self.phone_audio_stop()
        if self.phone is not None and self.phone.busy():
            self.phone.hush()
        remote = self.remote()
        if remote:
            remote.hush()
        with self._lock:
            self._kill()

    def test(self, voice=None, rate=None, silero_voice=None, grok_voice=None, engine=None, *, on_mac=False):
        """Пробная фраза выбранным голосом (из окна настроек)."""
        if self.muted():
            return                        # «Пауза голоса» — и пробная фраза молчит
        self.stop()
        preview_engine = engine if engine in ("system", "silero", "grok", "openai", "gemini") else None
        phone = None if on_mac else self._phone()
        if phone:
            self.selected_engine = preview_engine
            self.selected_rate = rate or self.rate
            if self.phone_audio and self.phone_audio(t("say.test")):
                return                    # Silero / Grok / OpenAI — звуком с Мака («Поток»)
            if preview_engine and preview_engine != "system":
                raise RuntimeError("The selected engine is not ready. Preview cancelled")
            phone.speak(t("say.test"), voice or self.voice, rate or self.rate)
            return
        if not audio_out.output_available():
            with self._lock:
                self._kill()
            self.log.warning("Speech: waiting for output “%s” to connect", audio_out.chosen("out"))
            return
        with self._lock:
            self._kill()
            # _kill clears the previous engine; select the preview only afterwards.
            self.selected_engine = preview_engine
            self.selected_rate = rate or self.rate
            remote = self.remote()
            if remote and remote.speak(t("say.test")):
                return
            if self._silero(t("say.test"), rate or self.rate, silero_voice):
                return
            if self._grok(t("say.test"), rate or self.rate, grok_voice):
                return
            if preview_engine and preview_engine != "system":
                raise RuntimeError("The selected engine is not ready. Preview cancelled")
            self._proc = subprocess.Popen(
                ["say", "-v", voice or self.voice, "-r", str(rate or self.rate), *audio_out.say_args(), "--",
                 t("say.test")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def _silero(self, text, rate=None, voice=None):
        """Silero выбран и текст русский — читает он. Вызывать под self._lock."""
        if self.local is None or self.engine() != "silero" or not self.local.suits(text):
            return False
        self.local.speak(text, voice=voice, rate=rate or self.playback_rate)
        return True

    def _grok(self, text, rate=None, voice=None):
        """Выбран голос Grok (или OpenAI по ключу API) — читает он. Вызывать под self._lock."""
        tts = self.cloud()
        if tts is None:
            return False
        tts.speak(text, voice=voice, rate=rate or self.playback_rate)
        return True

    def busy(self, phone=True):
        """Звучит голос. phone=False — озвучку на телефоне не считаем (она в наушниках, эха нет)."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        if time.time() < self.sound_until:
            return True
        if self.local is not None and self.local.busy():
            return True
        tts = self.cloud()
        if tts is not None and tts.busy():
            return True
        if phone and self.phone is not None and self.phone.busy():
            return True
        remote = self.remote()
        return bool(remote and remote.speaking())

    def busy_or_recent(self, tail=0.5, phone=True):
        """Говорит сейчас или замолчал меньше tail секунд назад (эхо в комнате)."""
        if self.busy(phone):
            self._last_busy = time.time()
            return True
        return time.time() - self._last_busy < tail

    def _kill(self):
        remote = self.remote()
        if remote:
            remote.hush()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        if self.local is not None:
            self.local.stop()
        tts = self.cloud()
        if tts is not None:
            tts.stop()
        self.selected_engine = None
        self.selected_rate = None




class GrokLogs:
    """Журналы Grok CLI (~/.grok/sessions/<папка>/<сессия>/chat_history.jsonl).

    Ход закончен — запись assistant с текстом и без вызова инструментов (финальный ответ).
    Папка проекта — в имени каталога (адрес, закодированный %XX).
    """

    ROOT = Path.home() / ".grok" / "sessions"
    DAYS = 3                    # файлы, менявшиеся за последние дни

    def __init__(self):
        self._files = {}        # путь -> {"offset", "cwd"}

    def _recent_files(self):
        if not self.ROOT.is_dir():
            return
        border = time.time() - self.DAYS * 86400
        for path in self.ROOT.glob("*/*/chat_history.jsonl"):
            try:
                if path.stat().st_mtime >= border:
                    yield path
            except OSError:
                continue

    def poll(self, first):
        """Новые финальные ответы: [{agent, cwd, state, message}]."""
        from urllib.parse import unquote
        events = []
        for path in self._recent_files():
            size = path.stat().st_size
            info = self._files.get(path)
            if info is None:
                # старые файлы при запуске не читаем, новые — с начала
                info = {"offset": size if first else 0, "cwd": unquote(path.parent.parent.name)}
                self._files[path] = info
            if size <= info["offset"]:
                continue
            with open(path, "rb") as f:
                f.seek(info["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1          # только целые строки
            info["offset"] += end
            for line in chunk[:end].splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                text = item.get("content")
                if item.get("type") == "assistant" and isinstance(text, str) and text.strip() and not item.get("tool_calls"):
                    events.append({"agent": "grok", "paneKey": None, "cwd": info["cwd"], "state": "done", "message": text,
                                   "path": str(path)})
        return events


class CodexLogs:
    """Журналы сессий Codex (~/.codex/sessions): событие task_complete = ход закончен.

    Нужен, потому что Orca не всегда видит статус Codex.
    """

    ROOT = Path.home() / ".codex" / "sessions"
    DAYS = 14

    def __init__(self, interim=lambda: False):
        self._files = {}   # путь -> {"offset": сколько байт уже прочитано, "cwd": папка сессии}
        self.interim = interim      # True — читать и промежуточные реплики (phase=commentary)

    def _recent_files(self):
        today = datetime.date.today()
        for back in range(self.DAYS):
            day = today - datetime.timedelta(days=back)
            folder = self.ROOT / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            if folder.is_dir():
                yield from folder.glob("*.jsonl")

    # сессии не из терминала (`codex exec`: разметка «Текста» Goblin Remote, «Нейро», скрипты) вслух не читаем —
    # иначе Voice озвучивал служебный JSON моделей как ответ агента (28.10); как SKIP_ENTRYPOINTS у Claude
    SKIP_ORIGINATORS = ("codex_exec",)

    @classmethod
    def _session_meta(cls, path):
        """(папка сессии, не из терминала ли)."""
        try:
            with open(path, encoding="utf-8") as f:
                p = json.loads(f.readline()).get("payload", {})
            source = p.get("source")
            subagent = (isinstance(source, dict) and "subagent" in source) or source == "subagent"
            subagent = subagent or str(p.get("agent_path") or "").startswith("/root/")
            return p.get("cwd", ""), bool(subagent or p.get("originator") in cls.SKIP_ORIGINATORS or source == "exec")
        except Exception:
            return "", False

    def poll(self, first):
        """Новые завершённые ходы: [{agent, cwd, state, message}]."""
        events = []
        for path in self._recent_files():
            size = path.stat().st_size
            info = self._files.get(path)
            if info is None:
                # старые файлы при запуске не читаем, новые — с начала
                cwd, skip = self._session_meta(path)
                info = {"offset": size if first else 0, "cwd": cwd, "skip": skip}
                self._files[path] = info
            if info.get("skip") or size <= info["offset"]:
                continue

            with open(path, "rb") as f:
                f.seek(info["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1          # только целые строки
            info["offset"] += end

            for line in chunk[:end].splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                payload = item.get("payload") or {}
                if item.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    events.append({
                        "agent": "codex",
                        "paneKey": None,
                        "cwd": info["cwd"],
                        "state": "done",
                        "message": payload.get("last_agent_message") or "",
                        "path": str(path),
                    })
                elif item.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
                    # Esc: ход прерван — ответа не будет (вслух не читаем, ядру — «агент работает» молчит)
                    events.append({"agent": "codex", "paneKey": None, "cwd": info["cwd"], "state": "stopped",
                                   "message": "", "path": str(path)})
                elif (item.get("type") == "response_item" and payload.get("type") == "message"
                      and payload.get("role") == "assistant" and payload.get("phase") == "commentary"
                      and self.interim()):
                    # промежуточная реплика (ход продолжается); итог (final_answer) придёт с task_complete
                    text = "\n".join(b.get("text", "") for b in payload.get("content") or []
                                     if isinstance(b, dict) and b.get("type") == "output_text").strip()
                    if text:
                        events.append({"agent": "codex", "paneKey": None, "cwd": info["cwd"],
                                       "state": "done", "message": text, "interim": True, "path": str(path)})
        return events


class ClaudeLogs:
    """Журналы сессий Claude Code (~/.claude/projects).

    Ход закончен, когда после ответа с stop_reason=end_turn появилась запись
    system/turn_duration (или stop_hook_summary). Запасной признак — файл молчит IDLE_S секунд.
    Журналы субагентов (isSidechain, папки subagents) не читаем.
    Сессии без терминала — `claude -p`, Agent SDK (entrypoint sdk-*): скрипты и проверки — тоже не читаем.
    """

    SKIP_ENTRYPOINTS = ("sdk",)       # entrypoint с этого начинается — сессия не из терминала
    REFUSAL = "The Claude session was stopped by the safeguards filter. Open a new tab or switch the model."

    ROOT = Path.home() / ".claude" / "projects"
    FRESH_S = 2 * 24 * 3600     # при запуске берём файлы, менявшиеся за двое суток
    RESCAN_S = 5                # список файлов обновляем раз в 5 с
    IDLE_S = 3

    def __init__(self, interim=lambda: False):
        self.interim = interim      # True — читать и промежуточные реплики (между вызовами инструментов)
        self._seen = set()          # (id сообщения, текст): промежуточную реплику читаем один раз
        self._since = time.time()   # файлы, созданные раньше, читаем только с конца
        self._files = {}            # путь -> {"offset", "pending", "cwd", "at"}
        self._paths = []
        self._scanned = 0.0

    def _recent(self):
        now = time.time()
        if now - self._scanned < self.RESCAN_S:
            return self._paths
        self._scanned = now
        found = []
        try:
            folders = [d.path for d in os.scandir(self.ROOT) if d.is_dir()]
        except OSError:
            folders = []
        for folder in folders:
            try:
                for f in os.scandir(folder):
                    if f.name.endswith(".jsonl") and f.is_file() and now - f.stat().st_mtime < self.FRESH_S:
                        found.append(f.path)
            except OSError:
                pass
        self._paths = found
        return found

    def _open(self, path, size):
        """Новый файл: созданный после запуска читаем с начала, старый — только новые строки."""
        try:
            born = os.stat(path).st_birthtime
        except (OSError, AttributeError):
            born = 0
        return {"offset": 0 if born >= self._since else size, "pending": [], "cwd": "", "at": 0.0, "entry": "",
                "path": str(path)}

    def poll(self):
        """Новые законченные ходы: [{agent, cwd, state, message}]."""
        events = []
        now = time.time()
        for path in self._recent():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            info = self._files.get(path)
            if info is None:
                info = self._files[path] = self._open(path, size)

            if size < info["offset"]:
                info["offset"] = size           # файл перезаписан
            elif size > info["offset"]:
                with open(path, "rb") as f:
                    f.seek(info["offset"])
                    chunk = f.read(size - info["offset"])
                end = chunk.rfind(b"\n") + 1      # только целые строки
                info["offset"] += end
                for line in chunk[:end].splitlines():
                    self._line(info, line, events)

            if info["pending"] and now - info["at"] > self.IDLE_S:
                events.append(self._done(info))
        return events

    def _line(self, info, raw, events):
        try:
            item = json.loads(raw)
        except ValueError:
            return
        if not isinstance(item, dict) or item.get("isSidechain"):
            return
        info["cwd"] = item.get("cwd") or info["cwd"]
        info["entry"] = item.get("entrypoint") or info.get("entry", "")
        if info["entry"].startswith(self.SKIP_ENTRYPOINTS):
            info["pending"] = []                # claude -p / SDK: вслух не читаем
            return
        kind = item.get("type")

        if kind == "user":
            info["pending"] = []                # новый вопрос — прошлый ответ уже не нужен
            content = (item.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            if any(isinstance(b, dict) and str(b.get("text") or "").startswith("[Request interrupted by user")
                   for b in blocks):
                # Esc: ход прерван — ответа не будет (вслух не читаем, ядру — «агент работает» молчит)
                events.append({"agent": "claude", "paneKey": None, "cwd": info["cwd"], "state": "stopped",
                               "message": "", "path": info.get("path")})
        elif kind == "assistant":
            message = item.get("message") or {}
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            text = "\n".join(b.get("text", "") for b in blocks
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if message.get("stop_reason") != "end_turn":
                # промежуточная реплика (ход продолжается) — сразу, если включено «Читать промежуточные»
                key = (message.get("id"), text)
                if text and self.interim() and key not in self._seen:
                    self._seen.add(key)
                    events.append({"agent": "claude", "paneKey": None, "cwd": info["cwd"], "state": "done", "message": text,
                                   "interim": True, "path": info.get("path")})
                return
            if text:
                info["pending"].append(text)
                info["at"] = time.time()
        elif kind == "system" and item.get("subtype") in ("turn_duration", "stop_hook_summary"):
            if info["pending"]:
                events.append(self._done(info))
        elif kind == "system" and item.get("subtype") == "model_refusal_no_fallback":
            # блок safeguards: дальше сессия не отвечает — сказать об этом, а не молчать
            info["pending"] = []
            events.append({"agent": "claude", "paneKey": None, "cwd": info["cwd"], "state": "done",
                           "message": self.REFUSAL, "refusal": True,
                           "path": info.get("path")})

    @staticmethod
    def _done(info):
        message = "\n\n".join(info["pending"])
        info["pending"] = []
        return {"agent": "claude", "paneKey": None, "cwd": info["cwd"], "state": "done", "message": message,
                "path": info.get("path")}


class AnswerWatcher(threading.Thread):
    """Раз в N секунд проверяет, какой агент закончил ход, и читает его ответ.

    Источники: Claude и OpenCode в Orca — `orca worktree ps`, Claude Code и Codex — их журналы.
    Чьи ответы читать — read_scope: all (любой агент) или active (только окно впереди).
    """

    def __init__(self, cfg, speaker, log, state):
        super().__init__(daemon=True)
        self.before_say = None      # вызывается перед озвучкой ответа (ядро ставит чтение файла на паузу)
        self.cfg = cfg
        self.speaker = speaker
        self.log = log
        self.state = state                 # общее состояние (speak_answers, last_target)
        self._prev = {}                    # paneKey -> прошлое состояние агента в Orca
        self._spoken = deque(maxlen=30)    # уже прочитанные ответы (без повторов)
        self._codex = CodexLogs(interim=lambda: bool(self.cfg.get("codex_interim", False)))
        self._grok = GrokLogs()
        self._claude = None                # журналы Claude: создаются при входе в прямой режим
        self._last_error = None            # последняя ошибка опроса (чтобы не повторять её в журнале)
        self.on_done = lambda ev: None     # ядру: агент закончил ответ или ждёт вас (blocked / waiting — Orca)
        self.skip = lambda ev: False       # ядру: ответ из диалога агентов («Оркестрация») — не читать и не показывать
        self.on_answer = lambda ev: None   # ядру: текст ответа агента (на планшет — даже если вслух не читаем)
        self.fast_source = lambda: []
        self._dispatch_lock = threading.RLock()
        self.route_codex = lambda events: events
        self.resolve_source = lambda ev: ev  # точные tty/PID из метаданных процесса
        self.is_leader = lambda ev: False
        self.front = lambda: ("any", None)   # что впереди (для read_scope=active) — задаёт ядро
        self._shown = deque(maxlen=30)     # уже показанные ответы
        self.inbox = deque()               # ответы «своих агентов» (POST /api/answer) — до ближайшего опроса
        self.history = {}                  # источник -> последние ответы [{agent, text, at}] (вкладка «Связка CLI»)
        self.talking = lambda: False       # «не перебивать диктовку»: человек сейчас говорит — задаёт ядро
        self.held = deque()                # отложенные фразы озвучки (по порядку)
        self.voice_for = None              # ev -> {"silero", "grok"} или None: свой голос агента (Джони)
        self._stop = threading.Event()
        from answer_hooks import AnswerHooks
        self.hooks = AnswerHooks(self)

    def stop(self):
        self._stop.set()

    def _fast_loop(self):
        """Completed TUI replies do not wait for filesystem/Orca journal scans."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                events = self.fast_source()
                discovered = time.monotonic()
                with self._dispatch_lock:
                    acquired = time.monotonic()
                    if events:
                        self.log.debug("Speech timing: source=%.3f s queue_wait=%.3f s",
                                       discovered - started, acquired - discovered)
                    self._release()
                    for ev in events:
                        self._remember(ev["agent"], ev)
                    enabled = self.cfg.get("sources") or {}
                    self._dispatch([ev for ev in events if enabled.get(ev["agent"], True)])
            except Exception as exc:
                self.log.debug("Fast speech source: %s", exc)
            self._stop.wait(max(.02, .5 - (time.monotonic() - started)))

    def run(self):
        threading.Thread(target=self.hooks.run, daemon=True, name="answer-hook-server").start()
        threading.Thread(target=self._fast_loop, daemon=True, name="terminal-speech").start()
        first = True
        while not self._stop.is_set():
            try:
                self._tick(first)
                first = False
                self._last_error = None
            except Exception as e:
                # одну и ту же ошибку (например, Orca закрыт) пишем один раз, а не при каждом опросе
                if str(e) != self._last_error:
                    self._last_error = str(e)
                    self.log.warning("watcher: %s", e)
            self._stop.wait(self.cfg.get("poll_seconds", 2.0))

    # ---------- источники событий

    def _orca_up(self):
        """Запущена ли Orca (проверка раз в 3 с, ~1 мс): без неё её CLI не зовём — он стартует ~0,15 с процессора."""
        now = time.time()
        if now - getattr(self, "_orca_at", 0) >= 3:
            self._orca_at = now
            try:
                import AppKit
                self._orca_on = any(a.bundleIdentifier() == "com.stablyai.orca" and not a.isTerminated()
                                    for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications())
            except Exception:
                self._orca_on = True
        return self._orca_on

    def _orca_events(self, first):
        """Агенты Orca, у которых только что сменилось состояние на done / blocked / waiting."""
        events = []
        if not self._orca_up():
            return events
        for wt in orca_cli.worktrees():
            for agent in wt.get("agents", []):
                key = agent.get("paneKey")
                prev = self._prev.get(key)
                self._prev[key] = agent
                if first or not prev:
                    continue

                state = agent.get("state")
                changed = (state != prev.get("state")
                           or agent.get("stateStartedAt") != prev.get("stateStartedAt"))
                if changed and state in ("done", "blocked", "waiting"):
                    agent_type = agent.get("agentType")
                    events.append({
                        "agent": "agy" if agent_type == "antigravity" else agent_type,
                        "paneKey": key,
                        "orcaSessionId": agent.get("orcaSessionId"),
                        "source": "orca",
                        "cwd": wt.get("path"),
                        "state": state,
                        "message": agent.get("lastAssistantMessage") or "",
                        "interrupted": agent.get("interrupted"),
                    })
        return events

    # ---------- чей это агент

    @staticmethod
    def _matches(event, term):
        if not term:
            return False
        if event.get("paneKey"):
            return event["paneKey"] == term.get("paneKey")
        if event.get("source") == "hook":
            return False
        # Codex: совпадает тип агента и папка проекта
        cwd, root = event.get("cwd") or "", term.get("cwd") or ""
        return term.get("agent") == event["agent"] and bool(root) and cwd.startswith(root)

    def _tick(self, first):
        """Следим за всеми источниками сразу: статусы Orca, журналы Claude Code и Codex.

        Чьи ответы читать вслух — настройка read_scope (не зависит от способа отправки):
        all — любой закончивший агент; active — только агент в окне/вкладке, которая сейчас впереди.
        """
        if self._claude is None:
            self._claude = ClaudeLogs(interim=lambda: bool(self.cfg.get("claude_interim", False)))
        try:
            orca = self._orca_events(first)
        except Exception:
            orca = []                       # Orca закрыта — это нормально, терминалы работают без неё
        # опрашиваем всё (чтобы после включения не всплыли старые ответы), берём только подключённые источники
        claude, codex = [], []
        try:
            claude = self._claude.poll()
        except Exception as e:
            self.log.debug("Claude log: %s", e)
        try:
            codex = self._codex.poll(first)
        except Exception as e:
            self.log.debug("Codex log: %s", e)
        try:
            grok = self._grok.poll(first)
        except Exception:
            grok = []
        custom = []
        while self.inbox:
            custom.append(self.inbox.popleft())
        orca, claude, codex, grok, custom = [
            [self.resolve_source(ev) for ev in batch]
            for batch in (orca, claude, codex, grok, custom)]
        codex = self.route_codex(codex)
        for key, evs in (("orca", orca), ("claude", claude), ("codex", codex), ("grok", grok)):
            for ev in evs:
                self._remember(key, ev)
        for ev in custom:
            self._remember("custom:" + ev["agent"], ev)
        linked = self.cfg.get("sources") or {}
        events = ((orca if linked.get("orca", True) else []) + (claude if linked.get("claude", True) else [])
                  + (codex if linked.get("codex", True) else []) + (grok if linked.get("grok", True) else []))
        if not linked.get("orca", True):
            events = [ev for ev in events if not (ev.get("paneKey") and ev.get("interim"))]
        self._dispatch(events + custom)

    def _dispatch(self, events):
        with self._dispatch_lock:
            # Completion tracking is independent of the selected speech transport.
            # In hook mode a remote TUI still proves which terminal finished.
            for ev in events:
                if (not ev.get("interim")
                        and ev.get("work_complete", True)
                        and ev.get("terminal_source") != "codex-tui-stream"
                        and ev.get("state") in ("done", "blocked", "waiting", "stopped")):
                    self.on_done(ev)
            hooks = self.cfg.get("answer_signal", "log") == "hooks"
            events = [ev for ev in events if not self.skip(ev) and (
                ev.get("custom") or ev.get("agent") not in HOOK_AGENTS
                or (ev.get("source") == "hook") == hooks
                or (hooks and ev.get("interim") and (ev.get("paneKey") or (ev.get("tty") and ev.get("pid")))
                    and self.cfg.get(ev.get("agent", "") + "_interim", False)))]
            if not events:
                return
            if self.cfg.get("read_scope", "all") == "leader":
                speak = [ev for ev in events if self.is_leader(ev)]
            elif self.cfg.get("read_scope", "all") == "active":
                front = self.front()
                speak = [ev for ev in events if self._is_front(ev, front)]
            else:
                speak = events
            self._speak(events, speak)

    HISTORY = 10

    def _remember(self, key, ev):
        """Последние ответы источника — для раскрывающегося журнала на вкладке «Связка CLI»."""
        text = (ev.get("message") or "").strip()
        if ev.get("state") != "done" or not text:
            return
        log = self.history.setdefault(key, deque(maxlen=self.HISTORY))
        log.append({"agent": ev.get("agent") or "", "text": text[:4000], "at": time.strftime("%H:%M:%S")})

    def push(self, agent, text, cwd=None, full=False):
        """Ответ «своего агента» (хук или команда шлёт его в Orca Voice) — прочитается при ближайшем опросе.
        full — читать целиком, без обрезки по лимиту (Сборщик: заголовки новинок уже короткие)."""
        self.inbox.append({"agent": agent, "paneKey": None, "cwd": cwd, "state": "done", "message": text, "custom": True,
                           "full": bool(full)})

    @classmethod
    def _is_front(cls, ev, front):
        """Агент этого события — в окне, которое сейчас впереди? front — от ядра (OrcaVoice.answer_front)."""
        kind, info = front
        if ev.get("custom") and not ev.get("cwd"):
            return True                     # свой агент без папки — вкладку не узнать, читаем
        if ev.get("paneKey"):
            return kind == "orca" and cls._matches(ev, info)
        if kind == "orca":
            return not ev.get("interim") and cls._matches(ev, info)
        if kind == "cwds":
            # вкладка Терминала / iTerm2: папки процессов этой вкладки — агент запущен ровно в одной из них
            # (не «внутри»: оболочка в домашней папке иначе подошла бы к любому проекту)
            cwd = (ev.get("cwd") or "").rstrip("/")
            return bool(cwd) and cwd in {d.rstrip("/") for d in info}
        return kind == "any"                # другой терминал впереди — вкладку не узнать, читаем

    @staticmethod
    def _norm(text):
        return re.sub(r"\s+", " ", text or "").strip()

    SAME_SEC = 10       # один ответ из Orca и из журнала агента приходит с разницей в секунды
    ORCA_EXACT_REPLY_SEC = 120

    def _seen(self, store, ev):
        """Связать копии ответа из разных источников и сохранить повторные ответы агента."""
        norm, now = self._norm(ev.get("message")), time.monotonic()
        pane = ev.get("paneKey")
        session = ev.get("orcaSessionId")
        interim = bool(ev.get("interim"))
        origin = ev.get("source") or ("orca" if pane else "log")
        location = ("pane", pane) if pane else ("terminal", ev.get("tty") or ev.get("path") or ev.get("cwd"))
        identity = (ev.get("agent"), location, interim)
        for old, at, owner, origins, old_session in store:
            if owner != identity or origin in origins or (session and old_session and session != old_session):
                continue
            age = now - at
            if (age < self.ORCA_EXACT_REPLY_SEC and norm == old and pane and session
                    and session == old_session and not interim and (origins | {origin}) == {"orca", "log"}):
                origins.add(origin)
                return True
            if age < self.SAME_SEC and (norm == old or (
                    min(len(norm), len(old)) >= 20 and (norm in old or old in norm))):
                origins.add(origin)
                return True
        store.append((norm, now, identity, {origin}, session))
        return False

    def _speak(self, events, speak):
        """Все события — ядру; очередь сохраняет порядок всех новых реплик."""
        for ev in events:
            self.log.debug("Speech source: agent=%s tty=%s pid=%s", ev.get("agent"),
                           ev.get("tty") or "unknown", ev.get("pid") or "unknown")
            self.log.debug("Agent %s: %s (%s), %d characters%s", ev.get("agent"), ev.get("state"), ev.get("source", "?"),
                           len(ev.get("message") or ""), "" if ev in speak else "; skipped because terminal is inactive or speech is disabled")
            self._show(ev)
        if not self.state["speak_answers"]:
            return
        done = [ev for ev in speak if ev["state"] == "done" and ev["message"].strip()]
        fresh = [ev for ev in done if not self._seen(self._spoken, ev)]
        if fresh:
            if self._queue():
                # Enqueue the entire batch before starting playback. Async TTS
                # may not report busy immediately after the first say().
                for ev in fresh:
                    self._say_done(ev, enqueue=True)
                self._release()
            else:
                self._say_done(fresh[-1])  # Queue disabled: keep latest-answer behaviour.
            return
        for ev in speak:
            if ev["state"] in ("blocked", "waiting") and not ev.get("interrupted") and self.cfg.get("announce_waiting", True):
                self.log.info(t("log.agent_waiting"), ev["agent"], ev["state"])
                self._say(Voiced.make(t("say.waiting"), None, dict(ev)))
                return

    def _show(self, ev):
        """Готовый ответ агента — ядру (оно отправит его на планшет), без повторов."""
        message = (ev.get("message") or "").strip()
        if ev.get("state") == "done" and message and not self._seen(self._shown, ev):
            try:
                self.on_answer(ev)
            except Exception as e:
                self.log.warning("answer: %s", e)

    def _say_done(self, ev, enqueue=False):
        message = ev["message"].strip()
        if message:
            # текст ответа — в той же строке после « ¶ » (JSON в одну строку): в журнале он раскрывается по клику
            self.log.info(t("log.reading") + " ¶ %s", ev["agent"], len(message), json.dumps(message, ensure_ascii=False))
            limit = self.cfg.get("max_speech_chars", 2500)
            if ev.get("full"):
                limit = max(limit, FULL_MAX)    # свой агент просил целиком (Сборщик) — только защита от огромного
            if ev.get("interim"):
                limit = self.cfg.get("interim_speech_chars", INTERIM_MAX)
                text = interim_for_speech(message, limit, self.cfg.get("interim_first_sentence", True))
                if len(text) < len(clean_for_speech(message, 10 ** 6)):
                    self.log.debug("Speech: partial reply limit %s, reading %d of %d characters", limit, len(text), len(message))
            else:
                text = clean_for_speech(message, limit, self.cfg.get("say_cut", True),
                                        self.cfg.get("speech_finish_phrase", True))
            if not ev.get("interim") and len(message) > limit:
                self.log.info("Speech: long reply (%d characters), reading %d characters", len(message), len(text))
            voices = self.voice_for(ev) if self.voice_for else None
            voiced = Voiced.make(text, voices, dict(ev))
            if enqueue:
                self.held.append(voiced)
                self.log.info(t("log.answer_held"), len(self.held))
            else:
                self._say(voiced)

    # ---------- «не перебивать диктовку»: пока человек говорит, озвучку откладываем

    def _hold(self):
        return bool(self.cfg.get("hold_while_dictating", True))

    def _queue(self):
        """«Очередь, пока голос говорит»: новый ответ ждёт, а не обрывает текущий на полуслове."""
        return bool(self.cfg.get("queue_while_speaking", False))

    def _say(self, text):
        """Озвучить сейчас или отложить: человек диктует, голос ещё говорит или уже есть очередь."""
        if self.cfg.get("read_scope") == "leader" and not self.is_leader(getattr(text, "event", None) or {}):
            return
        busy = self._queue() and self.speaker.busy()
        if (self._hold() or self._queue()) and (self.held or busy or (self._hold() and self.talking())):
            self.held.append(text)
            self.log.info(t("log.answer_held"), len(self.held))
            return
        if self.before_say:
            self.before_say()             # например, пауза чтения файла: ответ его перебивает
        self.speaker.say(text)

    def _release(self):
        """Человек замолчал, голос свободен — читаем следующую отложенную фразу."""
        if not self.held:
            return
        if not self._hold() and not self._queue():
            self.held.clear()
            return
        if (self._hold() and self.talking()) or self.speaker.busy():
            return
        text = self.held.popleft()
        if self.cfg.get("read_scope") == "leader" and not self.is_leader(getattr(text, "event", None) or {}):
            return
        self.log.info(t("log.answer_released"), len(self.held))
        self.speaker.say(text)

    def clear_held(self):
        """«Заткнись»: отложенные ответы больше не читаем."""
        if self.held:
            self.log.info(t("log.answer_dropped"), len(self.held))
            self.held.clear()
