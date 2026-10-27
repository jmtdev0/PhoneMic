"""Thread-safe jitter buffer between the network side and the audio callback."""

from __future__ import annotations

import threading

import numpy as np


class PcmQueue:
    """Mono int16 FIFO with a prebuffer, a hard size cap and underrun/overflow counters.

    The network side pushes whatever arrives; the audio callback pulls a fixed number of
    samples. Until `prebuffer_ms` of audio has accumulated the queue returns silence, and it
    goes back to buffering after an underrun, so short network hiccups do not turn into
    crackling.
    """

    def __init__(self, sample_rate: int, prebuffer_ms: int = 60, max_ms: int = 400) -> None:
        self.sample_rate = sample_rate
        self._prebuffer = int(sample_rate * prebuffer_ms / 1000)
        self._max = int(sample_rate * max_ms / 1000)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._playing = False
        self.underruns = 0
        self.dropped_samples = 0

    @property
    def buffered_samples(self) -> int:
        with self._lock:
            return len(self._buf) // 2

    @property
    def buffered_ms(self) -> int:
        return int(self.buffered_samples * 1000 / self.sample_rate)

    def push(self, data: bytes) -> None:
        """Append little-endian int16 samples; the oldest audio is dropped beyond the cap."""
        if len(data) % 2:
            data = data[:-1]
        with self._lock:
            self._buf += data
            overflow = len(self._buf) // 2 - self._max
            if overflow > 0:
                del self._buf[: overflow * 2]
                self.dropped_samples += overflow

    def pull(self, frames: int) -> np.ndarray:
        """Return exactly `frames` samples, padded with silence when there is not enough audio."""
        out = np.zeros(frames, dtype=np.int16)
        with self._lock:
            available = len(self._buf) // 2
            if not self._playing:
                if available < self._prebuffer:
                    return out
                self._playing = True
            take = min(frames, available)
            if take:
                chunk = bytes(self._buf[: take * 2])
                out[:take] = np.frombuffer(chunk, dtype="<i2")
                del self._buf[: take * 2]
            if take < frames:
                self.underruns += 1
                self._playing = False
        return out
