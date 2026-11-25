"""One continuous PCM output per utterance, isolated from Voice's microphone.
stdin: mono 24 kHz signed PCM16; optional continuous atempo filter.
"""
import math
import queue
import threading
import json
import shutil
import subprocess
import sys


def play(source, speed, device_name, manual):
    import sounddevice as sd
    device = None
    if manual:
        device = next((i for i, d in enumerate(sd.query_devices())
                       if d['name'] == device_name and d['max_output_channels'] > 0), None)
        if device is None:
            raise RuntimeError(f'Device not found: {device_name}')
    rate = int(sd.query_devices(device, 'output')['default_samplerate'])
    # One filter retains its state across all incoming chunks; no MP3 padding.
    p = subprocess.Popen([shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg',
        '-v', 'error', '-probesize', '32', '-analyzeduration', '0', '-f', 's16le', '-ar', '24000', '-ac', '1',
        '-i', 'pipe:0', '-filter:a', f'atempo={speed}',
        '-f', 's16le', '-ar', str(rate), '-ac', '1', 'pipe:1'],
        stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        # Read ahead independently of CoreAudio: network bursts must not directly
        # determine the timing of output writes. Bound queued audio to ~5 seconds.
        chunks = queue.Queue(maxsize=max(8, math.ceil(rate * 2 * 5 / 4096)))
        end = object()
        def receive():
            try:
                while True:
                    block = p.stdout.read(4096)
                    if not block:
                        break
                    chunks.put(block)
            except Exception as exc:
                chunks.put(exc)
            finally:
                chunks.put(end)
        threading.Thread(target=receive, daemon=True).start()
        pending = []
        queued = 0
        eof = False
        rebuffers = 0
        underflows = 0
        threshold = math.ceil(rate * 2 * 3.0)
        def refill(target):
            nonlocal queued, eof
            while queued < target and not eof:
                item = chunks.get()
                if item is end:
                    eof = True
                elif isinstance(item, Exception):
                    raise item
                else:
                    pending.append(item)
                    queued += len(item)
        # Do not open an empty hardware stream while waiting for first audio.
        refill(threshold)
        with sd.RawOutputStream(samplerate=rate, channels=1, dtype='int16',
                                device=device, latency='high') as out:
            while pending or not eof:
                if not pending:
                    try:
                        item = chunks.get_nowait()
                    except queue.Empty:
                        rebuffers += 1
                        refill(threshold)
                        continue
                    if item is end:
                        eof = True
                        continue
                    if isinstance(item, Exception):
                        raise item
                    pending.append(item)
                    queued += len(item)
                block = pending.pop(0)
                queued -= len(block)
                if out.write(block):
                    underflows += 1
            if p.wait(timeout=5):
                raise RuntimeError('Could not process the audio stream')
            out.write(bytes(2 * math.ceil(out.latency * rate)))
        print(json.dumps({'buffer_s': 3.0, 'rebuffers': rebuffers,
                          'underflows': underflows}), file=sys.stderr, flush=True)
    finally:
        if p.poll() is None:p.kill()
        p.wait();p.stdout.close()


if __name__ == '__main__':
    try:
        play(sys.stdin.buffer, float(sys.argv[3]), sys.argv[1], sys.argv[2] == 'True')
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
