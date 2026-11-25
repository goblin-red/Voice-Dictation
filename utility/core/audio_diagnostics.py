"""Bounded asynchronous recording before VAD; never opens another microphone."""
import json
import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from audio_archive import maintain
from audio_log_paths import prepare
from audio_resample import ArchiveResampler


class AudioDiagnostics:
    def __init__(self, folder, log, retention=86400, chunk_seconds=300):
        self.folder = Path(folder)
        prepare(self.folder)
        self.log = log
        self.retention = retention
        self.chunk_seconds = chunk_seconds
        self.enabled = False
        self.queue = queue.Queue(maxsize=200)
        self.stopping = threading.Event()
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name='audio-diagnostics')
        self.thread.start()
        self.archiver = threading.Thread(target=maintain, args=(self.folder, self.log, self.stopping, retention),
                                         daemon=True, name="audio-archive")
        self.archiver.start()

    def feed(self, samples, source):
        if not self.enabled or self.stopping.is_set():
            return
        try:
            self.queue.put_nowait((time.time(), samples.copy(), source))
        except queue.Full:
            self.dropped += 1

    def close(self):
        self.enabled = False
        self.stopping.set()
        self.thread.join(timeout=1)
        self.archiver.join(timeout=1)

    def prune(self):
        cutoff = time.time() - self.retention
        if self.folder.exists():
            for path in (p for directory in (self.folder, self.folder/'wav', self.folder/'flac', self.folder/'json')
                         for p in directory.glob('raw-*')):
                try:
                    if path.suffix in ('.wav', '.jsonl', '.flac', '.gz') and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass  # archive worker replaced a completed WAV

    def _run(self):
        wav = index = None
        started = previous = cleanup = 0
        source = None
        reported = 0
        frames = 0
        resampler = ArchiveResampler()
        def finish():
            nonlocal wav, index
            if wav:
                wav.close()
                wav = None
            if index:
                index.close()
                index = None
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    now = time.time()
                    if now - cleanup > 60:
                        self.prune()
                        cleanup = now
                    if reported != self.dropped:
                        self.log.warning('Audio diagnostics: queue full; dropped frames: %s', self.dropped)
                        reported = self.dropped
                    try:
                        stamp, samples, incoming = self.queue.get(timeout=0.2)
                    except queue.Empty:
                        finish()
                        continue
                    if wav and (stamp - started >= self.chunk_seconds or incoming != source or stamp - previous > .2):
                        finish()
                    if wav is None:
                        self.folder.mkdir(parents=True, exist_ok=True)
                        name = 'raw-' + datetime.fromtimestamp(stamp).strftime('%Y%m%d-%H%M%S-%f')
                        path = self.folder / 'wav' / name
                        wav = wave.open(str(path.with_suffix('.wav')), 'wb')
                        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                        resampler = ArchiveResampler()
                        index = (self.folder / 'json' / (name + '.jsonl')).open('w', encoding='utf-8')
                        index.write(json.dumps({'source': incoming, 'rate': 24000, 'input_rate': 16000, 'resample_delay_seconds': 0.000625, 'timestamp': 'Unix seconds at frame reception; not hardware ADC time'}, ensure_ascii=False) + '\n')
                        started, source, frames = stamp, incoming, 0
                        self.log.info('Audio diagnostics: %s; source=%s', path.with_suffix('.wav'), source)
                    samples = resampler.feed(samples)
                    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2')
                    wav.writeframes(pcm.tobytes())  # patches WAV header after each frame, even before shutdown
                    index.write(json.dumps({'received': stamp, 'offset': frames / 24000, 'samples': len(samples)}) + '\n')
                    index.flush()
                    frames += len(samples)
                    previous = stamp
                except Exception:
                    self.log.exception('Audio diagnostics: recording or cleanup failed')
                    finish()
                    self.enabled = False
                    self.stopping.wait(1)
        finally:
            finish()
