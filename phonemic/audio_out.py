"""Plays the incoming audio into a Windows playback device (VB-CABLE's "CABLE Input")."""

from __future__ import annotations

import sounddevice as sd

from .pcm_queue import PcmQueue


class DeviceNotFoundError(RuntimeError):
    """Raised when no matching output device exists."""


def _wasapi_index() -> int | None:
    for index, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return index
    return None


def output_devices() -> list[tuple[int, str]]:
    """(index, name) of every WASAPI playback device."""
    api = _wasapi_index()
    return [
        (index, device["name"])
        for index, device in enumerate(sd.query_devices())
        if device["hostapi"] == api and device["max_output_channels"] > 0
    ]


def find_output_device(name_part: str) -> tuple[int, str]:
    """Find a WASAPI playback device by case-insensitive name fragment.

    VB-CABLE also exposes a 16-channel input ("CABLE In 16ch"); it is skipped on purpose.
    """
    needle = name_part.lower()
    for index, name in output_devices():
        if needle in name.lower() and "16ch" not in name.lower():
            return index, name
    available = "\n".join(f"  [{index}] {name}" for index, name in output_devices())
    raise DeviceNotFoundError(
        f'No playback device matches "{name_part}". Available WASAPI devices:\n{available}'
    )


class AudioOutput:
    """Mono PCM in, stereo WASAPI shared-mode stream out."""

    def __init__(self, device_index: int, sample_rate: int, prebuffer_ms: int = 60) -> None:
        self.queue = PcmQueue(sample_rate, prebuffer_ms)
        self._stream = sd.OutputStream(
            device=device_index,
            samplerate=sample_rate,
            channels=2,
            dtype="int16",
            latency="low",
            callback=self._callback,
            # Lets Windows convert when the phone's sample rate differs from the device format.
            extra_settings=sd.WasapiSettings(auto_convert=True),
        )

    def _callback(self, outdata, frames, time, status) -> None:  # noqa: ARG002 - PortAudio signature
        mono = self.queue.pull(frames)
        outdata[:, 0] = mono
        outdata[:, 1] = mono

    def start(self) -> None:
        self._stream.start()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()
