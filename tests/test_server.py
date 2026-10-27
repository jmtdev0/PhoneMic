import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from phonemic.pcm_queue import PcmQueue
from phonemic.server import PairingGuard, create_app, is_local_address

CODE = "123456"


class FakeOutput:
    def __init__(self, sample_rate: int) -> None:
        self.queue = PcmQueue(sample_rate, prebuffer_ms=0)
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class PairingGuardTests(unittest.TestCase):
    def test_accepts_the_right_code_and_rejects_others(self):
        guard = PairingGuard(CODE)
        self.assertTrue(guard.check("10.0.0.2", CODE))
        self.assertFalse(guard.check("10.0.0.2", "000000"))

    def test_locks_an_address_after_repeated_failures_and_unlocks_later(self):
        now = [0.0]
        guard = PairingGuard(CODE, clock=lambda: now[0])
        for _ in range(5):
            guard.check("10.0.0.2", "bad")
        self.assertTrue(guard.locked("10.0.0.2"))
        self.assertFalse(guard.locked("10.0.0.3"))
        now[0] = 61.0
        self.assertFalse(guard.locked("10.0.0.2"))

    def test_only_loopback_and_private_addresses_are_local(self):
        for address in ("127.0.0.1", "192.168.1.20", "10.1.2.3", "172.16.0.9", "::1"):
            self.assertTrue(is_local_address(address), address)
        for address in ("8.8.8.8", "203.0.113.5", "", None, "not-an-ip"):
            self.assertFalse(is_local_address(address), address)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.outputs: list[FakeOutput] = []

        def factory(rate: int) -> FakeOutput:
            output = FakeOutput(rate)
            self.outputs.append(output)
            return output

        patcher = mock.patch("phonemic.server.AUTH_FAIL_DELAY_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(TestServer(create_app(CODE, factory)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def _hello(self, ws, code: str = CODE, rate: int = 48000):
        await ws.send_json({"type": "hello", "code": code, "sampleRate": rate})

    async def test_serves_the_phone_page(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("PhoneMic", await response.text())

    async def test_apk_is_served_when_built_and_404_otherwise(self):
        missing = await self.client.get("/app.apk")
        self.assertEqual(missing.status, 404)
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "app.apk"
            apk.write_bytes(b"PK\x03\x04fake")
            client = TestClient(TestServer(create_app(CODE, FakeOutput, apk_path=apk)))
            await client.start_server()
            try:
                response = await client.get("/app.apk")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "application/vnd.android.package-archive")
                self.assertEqual(await response.read(), b"PK\x03\x04fake")
            finally:
                await client.close()

    async def test_audio_reaches_the_output_queue(self):
        ws = await self.client.ws_connect("/ws")
        await self._hello(ws)
        self.assertEqual((await ws.receive_json())["type"], "ready")
        await ws.send_bytes(np.arange(960, dtype="<i2").tobytes())
        for _ in range(50):
            if self.outputs[0].queue.buffered_samples == 960:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.outputs[0].queue.buffered_samples, 960)
        self.assertTrue(self.outputs[0].started)
        await ws.close()
        for _ in range(50):
            if self.outputs[0].closed:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.outputs[0].closed)

    async def test_wrong_code_is_rejected_with_close_code_4401(self):
        ws = await self.client.ws_connect("/ws")
        await self._hello(ws, code="000000")
        self.assertEqual((await ws.receive_json())["type"], "error")
        message = await ws.receive()
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 4401)
        self.assertEqual(self.outputs, [])

    async def test_malformed_hello_is_rejected_with_close_code_4400(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_str("not json")
        await ws.receive()
        self.assertEqual(ws.close_code, 4400)

    async def test_out_of_range_sample_rate_is_rejected(self):
        ws = await self.client.ws_connect("/ws")
        await self._hello(ws, rate=1)
        await ws.receive()
        self.assertEqual(ws.close_code, 4400)

    async def test_a_newer_connection_replaces_the_older_one(self):
        first = await self.client.ws_connect("/ws")
        await self._hello(first)
        await first.receive_json()
        second = await self.client.ws_connect("/ws")
        await self._hello(second)
        self.assertEqual((await second.receive_json())["type"], "ready")
        await first.receive()
        self.assertEqual(first.close_code, 4409)
        self.assertTrue(self.outputs[0].closed)
        self.assertFalse(self.outputs[1].closed)

    async def test_too_many_wrong_codes_lock_the_address_out(self):
        for _ in range(5):
            ws = await self.client.ws_connect("/ws")
            await self._hello(ws, code="bad")
            await ws.receive()
            await ws.receive()
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect("/ws")
        self.assertEqual(caught.exception.status, 429)


if __name__ == "__main__":
    unittest.main()
