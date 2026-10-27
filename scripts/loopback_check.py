"""Hardware loopback check of the PC side, without a phone.

A 1 kHz tone is sent to the PhoneMic server over a WebSocket exactly like the phone page does. The server plays
it into "CABLE Input"; this script records "CABLE Output" and verifies that the tone arrives, then reports the
approximate delay. Run from the project root: python -m scripts.loopback_check
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time

import numpy as np
import sounddevice as sd
from aiohttp import ClientSession, web

from phonemic.audio_out import AudioOutput, find_output_device
from phonemic.server import create_app

RATE = 48000
CHUNK = 960  # 20 ms, the same as the phone page
TONE_HZ = 1000
SECONDS = 3
AMPLITUDE = 0.3
CODE = "123456"


def find_input_device(name_part: str) -> tuple[int, str]:
    wasapi = next(i for i, api in enumerate(sd.query_hostapis()) if "WASAPI" in api["name"])
    for index, device in enumerate(sd.query_devices()):
        if device["hostapi"] == wasapi and device["max_input_channels"] > 0 and name_part.lower() in device["name"].lower():
            return index, device["name"]
    raise SystemExit(f'No WASAPI recording device matches "{name_part}".')


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def run() -> int:
    out_index, out_name = find_output_device("CABLE Input")
    in_index, in_name = find_input_device("CABLE Output")
    print(f"Playing into : {out_name}\nRecording from: {in_name}")

    runner = web.AppRunner(create_app(CODE, lambda rate: AudioOutput(out_index, rate, prebuffer_ms=60)))
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    blocks: list[tuple[float, np.ndarray]] = []

    def on_audio(indata, frames, time_info, status) -> None:  # noqa: ARG001 - PortAudio signature
        blocks.append((time.monotonic(), indata[:, 0].copy()))

    sent_at = 0.0
    with sd.InputStream(
        device=in_index, samplerate=RATE, channels=2, dtype="float32", callback=on_audio,
        extra_settings=sd.WasapiSettings(auto_convert=True),
    ):
        async with ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            await ws.send_json({"type": "hello", "code": CODE, "sampleRate": RATE})
            if (await ws.receive_json())["type"] != "ready":
                print("FAIL: the server did not accept the connection")
                return 1
            start = sent_at = time.monotonic()
            for i in range(SECONDS * RATE // CHUNK):
                t = (np.arange(CHUNK) + i * CHUNK) / RATE
                pcm = (np.sin(2 * np.pi * TONE_HZ * t) * AMPLITUDE * 32767).astype("<i2")
                await ws.send_bytes(pcm.tobytes())
                await asyncio.sleep(max(0.0, start + (i + 1) * CHUNK / RATE - time.monotonic()))
            await asyncio.sleep(0.4)
    await runner.cleanup()

    audio = np.concatenate([block for _, block in blocks])
    first_loud = next((stamp for stamp, block in blocks if np.sqrt(np.mean(block**2)) > 0.05), None)
    window = audio[RATE // 2 : RATE // 2 + RATE]  # one second from the middle of the tone
    rms = float(np.sqrt(np.mean(window**2)))
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    peak_hz = float(np.argmax(spectrum) * RATE / len(window))
    delay_ms = None if first_loud is None else (first_loud - sent_at) * 1000

    print(f"Recorded {len(audio) / RATE:.1f} s | RMS {rms:.3f} (sent ≈ {AMPLITUDE / np.sqrt(2):.3f}) | dominant {peak_hz:.0f} Hz")
    if delay_ms is not None:
        print(f"Approximate delay: {delay_ms:.0f} ms (includes the 60 ms prebuffer and the recording block)")
    ok = abs(peak_hz - TONE_HZ) < 20 and rms > 0.05
    print("PASS: the tone came out of CABLE Output." if ok else "FAIL: the tone did not arrive as expected.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
