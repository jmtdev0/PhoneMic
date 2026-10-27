import unittest

import numpy as np

from phonemic.pcm_queue import PcmQueue


def pcm(values: list[int]) -> bytes:
    return np.array(values, dtype="<i2").tobytes()


class PcmQueueTests(unittest.TestCase):
    def test_returns_silence_until_prebuffer_is_reached(self):
        queue = PcmQueue(sample_rate=1000, prebuffer_ms=100)  # 100 samples
        queue.push(pcm([7] * 50))
        self.assertTrue((queue.pull(10) == 0).all())
        queue.push(pcm([7] * 50))
        self.assertTrue((queue.pull(10) == 7).all())

    def test_pads_with_silence_and_rebuffers_after_underrun(self):
        queue = PcmQueue(sample_rate=1000, prebuffer_ms=10)  # 10 samples
        queue.push(pcm([5] * 10))
        out = queue.pull(15)
        self.assertEqual(list(out[:10]), [5] * 10)
        self.assertEqual(list(out[10:]), [0] * 5)
        self.assertEqual(queue.underruns, 1)
        queue.push(pcm([9] * 4))  # below the prebuffer again
        self.assertTrue((queue.pull(4) == 0).all())

    def test_drops_the_oldest_audio_beyond_the_cap(self):
        queue = PcmQueue(sample_rate=1000, prebuffer_ms=0, max_ms=10)  # cap: 10 samples
        queue.push(pcm(list(range(1, 16))))
        self.assertEqual(queue.buffered_samples, 10)
        self.assertEqual(queue.dropped_samples, 5)
        self.assertEqual(list(queue.pull(10)), list(range(6, 16)))

    def test_ignores_a_trailing_odd_byte(self):
        queue = PcmQueue(sample_rate=1000, prebuffer_ms=0)
        queue.push(pcm([1, 2]) + b"\x00")
        self.assertEqual(queue.buffered_samples, 2)


if __name__ == "__main__":
    unittest.main()
