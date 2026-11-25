"""Диктовка «Apple»: распознаватель речи macOS (локально, потоком) через программу lib/bin/apple-stt.

Детектор речи (Listener) открывает фразу (start), досылает звук (feed) и закрывает её (end);
текст по ходу речи приходит в on_partial(id, text), окончательный — ждёт finish(id).
"""

import json
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from libdir import lib_dir

HERE = Path(__file__).resolve().parent
FINAL_WAIT_S = 4.0          # сколько ждать окончательный текст после конца фразы
LOCALES = {"ru": "ru-RU", "en": "en-US"}


class AppleSTT:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.on_partial = None          # функция (id, text) — текст по ходу речи (из потока чтения)
        self.error = None               # почему не работает (для статуса)
        self._proc = None
        self._locale = None
        self._seq = 0
        self._open = None               # id фразы, в которую сейчас идёт звук
        self._finals = {}               # id -> окончательный текст
        self._dropped = set()           # фразы, чей текст уже не нужен
        self._last = {}                 # id -> последний текст по ходу речи (если окончательный не придёт)
        self._glued = {}                # id -> (склеенные прошлые обрывки, последний текст Apple)
        self._final_ready = threading.Condition()
        self._lock = threading.Lock()   # запись в программу

    # ---------- программа

    def binary(self):
        return lib_dir(self.cfg) / "bin" / "apple-stt"

    def _build(self):
        """Программы нет — собрать из tools/apple_stt.swift (нужен swiftc)."""
        if not shutil.which("swiftc"):
            raise RuntimeError("swiftc is missing: install Command Line Tools (xcode-select --install)")
        self.log.info("Apple: building speech recognition…")
        subprocess.run([str(HERE.parent / "tools" / "build-apple-stt.sh"), str(self.binary())],
                       check=True, capture_output=True, timeout=300)

    def set_hints(self, words):
        """Слова команд — подсказка распознавателю (иначе короткое «рекс» он не узнаёт)."""
        self._hints = [w for w in dict.fromkeys(words) if w][:100]
        self._send(b"C", "\n".join(self._hints).encode())

    def ensure(self, lang, punctuation=True):
        """Запустить программу для языка lang (ru / en); уже запущена с теми же настройками — ничего."""
        locale = LOCALES.get(lang, "ru-RU")
        args = [locale] + ([] if punctuation else ["nopunct"])
        if self._proc and self._proc.poll() is None and self._locale == args:
            return
        self.stop()
        if not self.binary().exists():
            self._build()
        self._proc = subprocess.Popen([str(self.binary()), *args], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._locale = args
        self.error = None
        first = json.loads(self._proc.stdout.readline() or "{}")   # ждём «готов» (или отказ)
        if not first.get("ready"):
            self.error = first.get("error") or "could not start"
            self.stop()
            raise RuntimeError(self.error)
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
        if getattr(self, "_hints", None):
            self.set_hints(self._hints)        # перезапуск — подсказку заново
        self.log.info("Apple: speech recognizer ready (%s)", locale)

    def stop(self):
        proc, self._proc = self._proc, None
        self._open = None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    def _send(self, kind, payload):
        proc = self._proc
        if not proc or proc.poll() is not None:
            return False
        try:
            with self._lock:
                proc.stdin.write(kind + struct.pack(">I", len(payload)) + payload)
                proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _read(self, proc):
        """Streaming чтения: текст по ходу речи — в on_partial, окончательный — в _finals."""
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            sid = msg.get("id")
            if not sid:
                continue
            if sid in self._dropped:
                if msg.get("final"):
                    self._dropped.discard(sid)
                    self._glued.pop(sid, None)
                continue
            text = self.glue(sid, msg.get("text") or "", bool(msg.get("final")))
            if msg.get("final"):
                with self._final_ready:
                    self._finals[sid] = text
                    self._final_ready.notify_all()
            else:
                self._last[sid] = text
            if not msg.get("final") and self.on_partial:
                try:
                    self.on_partial(sid, text)
                except Exception as e:
                    self.log.warning("Apple: %s", e)
        self.log.info("Apple: speech recognizer exited")

    # ---------- склейка: Apple после паузы начинает текст фразы заново

    @staticmethod
    def _words(text):
        """Слова для сравнения: без знаков и регистра."""
        return re.sub(r"[^\w\s]+", " ", text.lower().replace("ё", "е")).split()

    @classmethod
    def _head(cls, text):
        """Начало текста для сравнения: первое слово, первые 4 буквы."""
        words = cls._words(text)
        return words[0][:4] if words else ""

    @classmethod
    def restarted(cls, prev, text):
        """Новый текст — не правка прошлого, а начатый заново обрывок (прошлый текст из него пропал)."""
        if not prev or not text or len(prev.split()) < 2:
            return False
        return cls._head(text) != cls._head(prev) and len(text) < len(prev) * 0.7

    def glue(self, sid, text, final=False):
        """Полный текст фразы: прошлые обрывки + текущий. Пустой окончательный — последний текст по ходу."""
        done, prev = self._glued.get(sid, ("", ""))
        if final and text and self._words(prev)[-len(self._words(text)):] == self._words(text):
            text = prev                  # итог — хвост уже распознанного (Apple оставил последний обрывок)
        elif self.restarted(prev, text):
            self.log.debug("Apple restarted the phrase — joining: %r + %r", prev, text)
            done = (done + " " + prev).strip()
        elif not text and final:
            text = prev                  # итог пустой — берём последний текст по ходу речи
        if final:
            self._glued.pop(sid, None)
        else:
            self._glued[sid] = (done, text)
        return (done + " " + text).strip()

    # ---------- фразы (вызывает детектор речи)

    def start(self, samples):
        """Новая фраза; samples — звук от её начала (float32). Возвращает id или None."""
        self._seq += 1
        sid = f"p{self._seq}"
        if not self._send(b"S", sid.encode()):
            return None
        self._open = sid
        self.feed(samples)
        return sid

    def feed(self, samples):
        if self._open:
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            self._send(b"A", pcm)

    def end(self):
        """Фраза кончилась: просим окончательный текст. Возвращает её id."""
        sid, self._open = self._open, None
        if sid:
            self._send(b"E", sid.encode())
        return sid

    def discard(self, sid):
        """Текст фразы не нужен (её забыли)."""
        self._last.pop(sid, None)
        self._glued.pop(sid, None)
        with self._final_ready:
            if self._finals.pop(sid, None) is None:
                self._dropped.add(sid)

    def finish(self, sid, timeout=FINAL_WAIT_S):
        """Окончательный текст фразы (ждём до timeout); не пришёл — последний текст по ходу речи."""
        deadline = time.time() + timeout
        with self._final_ready:
            while sid not in self._finals:
                left = deadline - time.time()
                if left <= 0:
                    self._dropped.add(sid)      # опоздавший текст выбросим
                    self.log.warning("Apple: no final transcript — using the interim transcript")
                    return self._last.pop(sid, "")
                self._final_ready.wait(left)
            self._last.pop(sid, None)
            return self._finals.pop(sid)
