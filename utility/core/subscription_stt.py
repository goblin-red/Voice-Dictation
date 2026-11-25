"""Recognize one recorded phrase through the existing OpenAI subscription transport."""
import io
import threading
import time
import wave

import numpy as np

from live import LiveSession, RATE, CHUNK


class SubscriptionSTT:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self._session = None
        self._epoch = 0

    def stop(self):
        self._epoch += 1
        session, self._session = self._session, None
        if session:
            session.stop()

    def transcribe(self, wav):
        with wave.open(io.BytesIO(wav), 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, RATE):
                raise ValueError('OpenAI: expected 16 kHz mono PCM16')
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32) / 32768
        epoch = self._epoch
        ready, done = threading.Event(), threading.Event()
        texts, errors = [], []
        last_text = [0.0]

        def on_text(text):
            texts.append(text)
            last_text[0] = time.monotonic()
            done.set()

        def on_state(state, detail):
            if state == 'live':
                ready.set()
            elif state in ('error', 'closed'):
                errors.append(str(detail or state))
                ready.set()
                done.set()

        session = LiveSession(on_text, on_state, self.log,
                              lang=self.cfg.get('language', 'ru'), prompt=self.cfg.get('live_prompt', ''))
        self._session = session

        def check():
            if epoch != self._epoch:
                raise RuntimeError('OpenAI: phrase cancelled')
            if errors:
                raise RuntimeError(errors[0])

        try:
            session.start()
            deadline = time.monotonic() + 25
            while not ready.wait(.05):
                check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('OpenAI: connection not ready')
            check()
            # Pace audio, then continue silence so the remote VAD can finish the phrase.
            for offset in range(0, len(samples), CHUNK):
                check()
                session.feed(samples[offset:offset + CHUNK])
                time.sleep(CHUNK / RATE)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                check()
                session.feed(np.zeros(CHUNK, dtype=np.float32))
                time.sleep(CHUNK / RATE)
                if done.is_set() and time.monotonic() - last_text[0] >= 1.5:
                    return ' '.join(texts).strip()
            raise TimeoutError('OpenAI: final transcript did not arrive')
        finally:
            if self._session is session:
                self._session = None
            session.stop()
