"""Проигрыватель звука на выбранное устройство — отдельным процессом.

Зачем отдельный процесс: CoreAudio иногда виснет намертво при открытии / закрытии потока вывода (PortAudio).
Зависнет этот маленький процесс — Voice его убьёт и продолжит работать (раньше висел сам Voice).
Запуск: python audio_play.py <файл> <устройство> <громкость>
Устройство не найдено или не играет — молчим; переход на системный выход запрещён.
"""

import math
import os
import sys
import time
import subprocess
import tempfile
from pathlib import Path


def read_audio(path, soundfile):
    """M4A и прочие форматы при необходимости декодируем, сохраняя выбранный выход."""
    try:
        return soundfile.read(path, dtype="float32", always_2d=True)
    except (RuntimeError, ValueError):
        with tempfile.TemporaryDirectory(prefix="goblin-audio-") as folder:
            decoded = str(Path(folder) / "decoded.wav")
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEF32", path, decoded],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            return soundfile.read(decoded, dtype="float32", always_2d=True)


def main():
    path, device_name, volume = sys.argv[1], sys.argv[2], sys.argv[3]
    if os.environ.get("AUDIO_PLAY_TEST_HANG"):      # для теста сторожа: «завис намертво»
        time.sleep(3600)
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf

        devices = sd.query_devices()
        device = next((i for i, d in enumerate(devices)
                       if d["name"] == device_name and d["max_output_channels"] > 0), None)
        if device is None:
            sys.stderr.write(f"Device not found: {device_name}\n")
            return 3
        data, rate = read_audio(path, sf)
        # Bluetooth-гарнитура с работающим микрофоном может принимать только моно.
        # Системные сигналы macOS (например Pop.aiff) — стерео: без сведения
        # OutputStream отказывает с Invalid number of channels, и Enter звучит тишиной.
        if devices[device]["max_output_channels"] == 1 and data.shape[1] > 1:
            data = data.mean(axis=1, keepdims=True)
        data = np.clip(data * float(volume), -1, 1)
        with sd.OutputStream(samplerate=rate, channels=data.shape[1], dtype="float32", device=device) as out:
            step = 2048
            for i in range(0, len(data), step):
                out.write(data[i:i + step])
            # Короткий сигнал целиком помещается в задержку Bluetooth: закрытие
            # потока может опередить звук на устройстве даже после успешного stop().
            # Продолжаем тот же поток тишиной ПОСЛЕ сигнала, на время его задержки.
            if len(data) / rate < out.latency:
                remaining = math.ceil(out.latency * rate)
                silence = np.zeros((step, data.shape[1]), dtype="float32")
                while remaining > 0:
                    count = min(step, remaining)
                    out.write(silence[:count])
                    remaining -= count
    except Exception as e:
        sys.stderr.write(f"Playback failed: {e}\n")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
