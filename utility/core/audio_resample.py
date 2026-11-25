"""Streaming 16→24 kHz archive conversion, with state across input blocks."""
import numpy as np

class ArchiveResampler:
    def __init__(self):
        n = np.arange(61) - 30
        h = np.sinc(n / 3) * np.kaiser(61, 5)
        self.h = h * (3 / h.sum())
        self.history = np.zeros(60)
        self.phase = 0

    def feed(self, samples):
        expanded = np.zeros(len(samples) * 3)
        expanded[::3] = samples
        joined = np.concatenate((self.history, expanded))
        filtered = np.convolve(joined, self.h, mode='valid')
        self.history = joined[-60:]
        result = filtered[self.phase::2]
        self.phase = (self.phase - len(expanded)) % 2
        return result
