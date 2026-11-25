"""whisper-server (whisper.cpp): запускаем один раз, модель остаётся в памяти — как в jauvex."""

import json
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from i18n import t
from libdir import find_model, lib_dir

HERE = Path(__file__).resolve().parent

# пустой результат, "...", "[BLANK_AUDIO]", "(шум)" — не речь (фильтр из jauvex)
NOISE = re.compile(r"^[\s.,!?¡¿-]*$|^\s*[\[(][^\])]*[\])]\s*$")
RETRY_S = 30     # whisper-server не запустился или упал: снова пробуем не чаще (при очередной фразе для него)


class WhisperServer:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.port = cfg["whisper_port"]
        self.proc = None
        self.error = None        # почему не запустился (для журнала и повторной попытки)
        self._tried = 0.0        # когда последний раз запускали
        self._managed = False    # сервер запускаем мы (Voice); прогон «Тестов» ходит к уже запущенному — не трогаем

    # ---------- запуск / остановка

    def _find_binary(self):
        """whisper-server: из lib, потом свой (его кладёт установщик), потом из PATH и Homebrew."""
        candidates = [
            lib_dir(self.cfg) / "bin" / "whisper-server",
            HERE.parent / "bin" / "whisper-server",         # старая установка: bin рядом с программой (utility/bin)
            shutil.which("whisper-server"),
            "/opt/homebrew/bin/whisper-server",
            "/usr/local/bin/whisper-server",
        ]
        return next((str(p) for p in candidates if p and Path(p).exists()), None)

    def start(self):
        self._tried = time.time()
        self._managed = True
        binary = self._find_binary()
        if not binary:
            raise RuntimeError(t("err.no_whisper"))

        # путь к модели можно задавать коротко: models/ggml-small-q5_1.bin (ищется в lib, потом рядом)
        model = find_model(self.cfg, self.cfg["model"])
        if not model.exists():
            raise RuntimeError(t("err.no_model", model))

        self._kill_leftovers()

        args = [
            binary,
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-l", self.cfg.get("language", "ru"),
            "-nt",   # без таймкодов
            "-sns",  # без неречевых токенов
        ]
        # подсказка словаря: server берёт её только при старте
        if self.cfg.get("prompt"):
            args += ["--prompt", self.cfg["prompt"]]

        self.log.info(t("log.whisper_starting"), model)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # ждём, пока сервер ответит (до 60 с)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(t("err.whisper_crashed", self.proc.returncode))
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)
                self.log.info(t("log.whisper_ready"), self.port)
                self.error = None
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(t("err.whisper_timeout"))

    def restart(self, cfg):
        """Перезапуск с новыми настройками (модель, язык, подсказка, порт)."""
        self.stop()
        self.cfg = cfg
        self.port = cfg["whisper_port"]
        self.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _kill_leftovers(self):
        """Гасит наш старый whisper-server на этом же порту (после падения прошлого запуска)."""
        subprocess.run(
            ["pkill", "-f", f"whisper-server.*--port {self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def ensure(self):
        """Сторож: whisper-server должен работать. Не запущен (сбой при старте Voice) или упал (нехватка памяти,
        убит) — запускаем снова, но не чаще раза в RETRY_S; в промежутке — ошибка сразу, без ожидания."""
        if not self._managed or (self.proc is not None and self.proc.poll() is None):
            return
        if time.time() - self._tried < RETRY_S:
            raise RuntimeError(self.error or "Whisper server is not running")
        if self.proc is not None:
            self.log.warning("Whisper server stopped (code %s). Restarting it", self.proc.returncode)
        else:
            self.log.info("Whisper server is not running. Starting it")
        try:
            self.start()
        except Exception as e:
            self.error = str(e)
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()                 # не дождались ответа — не оставлять полузапущенный
            raise

    # ---------- распознавание

    def transcribe(self, wav_bytes):
        """WAV (16 кГц, моно) -> текст. Пустая строка, если это не речь."""
        self.ensure()
        boundary = uuid.uuid4().hex
        fields = {
            "response_format": "json",
            "temperature": "0.0",
            "language": self.cfg.get("language", "ru"),
        }

        body = b""
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="u.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:   # кусок диктовки бывает до 5 минут
            text = json.loads(resp.read()).get("text", "")

        text = re.sub(r"\s+", " ", text).strip()
        return "" if NOISE.match(text) else text
