"""End to end over real sockets: pair with a QR token, connect with a signature, get revoked."""

import asyncio
import base64
import unittest
from unittest import mock

import numpy as np
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from phonemic.admin import CSRF_HEADER, create_admin_app, pairing_link
from phonemic.devices import DeviceRegistry, EnrollmentTokens
from phonemic.pcm_queue import PcmQueue
from phonemic.server import STATE, ServerInfo, create_app
from tests.test_devices import FakePhone

CODE = "123456"
PIN = "ab" * 32


class FakeOutput:
    def __init__(self, sample_rate: int) -> None:
        self.queue = PcmQueue(sample_rate, prebuffer_ms=0)
        self.closed = False

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class PairingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.outputs: list[FakeOutput] = []

        def factory(rate: int) -> FakeOutput:
            output = FakeOutput(rate)
            self.outputs.append(output)
            return output

        patcher = mock.patch("phonemic.server.AUTH_FAIL_DELAY_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = ServerInfo(name="JMTDesktop25", port=8443, pin_hex=PIN, ips=("192.168.1.38",))
        self.app = create_app(CODE, factory, registry=DeviceRegistry(), enrollment=EnrollmentTokens(), info=self.info)
        self.state = self.app[STATE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.phone = FakePhone()

    async def _enroll(self, token: str, phone: FakePhone | None = None, name: str = "S20 FE de Javier"):
        phone = phone or self.phone
        return await self.client.post(
            "/enroll",
            json={"token": token, "name": name, "publicKey": base64.b64encode(phone.public_key_der).decode()},
        )

    async def _pair(self) -> str:
        response = await self._enroll(self.state.enrollment.create())
        self.assertEqual(response.status, 200)
        return (await response.json())["deviceId"]

    async def _connect_as(self, device_id: str, phone: FakePhone | None = None):
        phone = phone or self.phone
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "device": device_id, "sampleRate": 48000})
        challenge = await ws.receive_json()
        self.assertEqual(challenge["type"], "challenge")
        signature = base64.b64encode(phone.sign(challenge["nonce"])).decode()
        await ws.send_json({"type": "auth", "signature": signature})
        return ws

    async def test_pair_then_connect_and_stream_audio_without_any_code(self):
        device_id = await self._pair()
        self.assertEqual(self.state.registry.get(device_id).name, "S20 FE de Javier")
        ws = await self._connect_as(device_id)
        self.assertEqual((await ws.receive_json())["type"], "ready")
        await ws.send_bytes(np.arange(960, dtype="<i2").tobytes())
        for _ in range(50):
            if self.outputs and self.outputs[0].queue.buffered_samples == 960:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.outputs[0].queue.buffered_samples, 960)
        self.assertIsNotNone(self.state.registry.get(device_id).last_seen)

    async def test_the_enrollment_response_names_the_pc(self):
        response = await self._enroll(self.state.enrollment.create())
        self.assertEqual((await response.json())["name"], "JMTDesktop25")

    async def test_a_token_cannot_be_reused(self):
        token = self.state.enrollment.create()
        self.assertEqual((await self._enroll(token)).status, 200)
        self.assertEqual((await self._enroll(token, FakePhone())).status, 403)
        self.assertEqual(len(self.state.registry.list()), 1)

    async def test_an_expired_token_is_refused(self):
        now = [0.0]
        self.state.enrollment = EnrollmentTokens(ttl_seconds=300, clock=lambda: now[0])
        token = self.state.enrollment.create()
        now[0] = 301.0
        self.assertEqual((await self._enroll(token)).status, 403)

    async def test_malformed_enrollments_are_bad_requests_and_do_not_burn_the_token(self):
        token = self.state.enrollment.create()
        for body in ({}, {"token": token}, {"token": token, "publicKey": "!!!"}, {"token": token, "publicKey": "AAAA"}, {"token": 5, "publicKey": "AAAA"}):
            response = await self.client.post("/enroll", json=body)
            self.assertEqual(response.status, 400, body)
        self.assertEqual((await self._enroll(token)).status, 200)

    async def test_guessing_tokens_locks_the_address_out(self):
        for _ in range(5):
            self.assertEqual((await self._enroll("wrong")).status, 403)
        self.assertEqual((await self._enroll(self.state.enrollment.create())).status, 429)

    async def test_a_phone_with_another_key_cannot_pass_as_a_paired_one(self):
        device_id = await self._pair()
        ws = await self._connect_as(device_id, phone=FakePhone())
        self.assertEqual((await ws.receive_json())["reason"], "bad signature")
        await ws.receive()
        self.assertEqual(ws.close_code, 4401)
        self.assertEqual(self.outputs, [])

    async def test_an_unknown_device_is_refused_with_4403(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "device": "deadbeef", "sampleRate": 48000})
        self.assertEqual((await ws.receive_json())["reason"], "unknown device")
        await ws.receive()
        self.assertEqual(ws.close_code, 4403)

    async def test_a_challenge_cannot_be_replayed(self):
        device_id = await self._pair()
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "device": device_id, "sampleRate": 48000})
        first = await ws.receive_json()
        await ws.send_json({"type": "auth", "signature": base64.b64encode(self.phone.sign(first["nonce"])).decode()})
        await ws.receive_json()  # ready
        await ws.close()

        ws2 = await self.client.ws_connect("/ws")
        await ws2.send_json({"type": "hello", "device": device_id, "sampleRate": 48000})
        second = await ws2.receive_json()
        self.assertNotEqual(first["nonce"], second["nonce"])
        stale = base64.b64encode(self.phone.sign(first["nonce"])).decode()  # signed for the previous challenge
        await ws2.send_json({"type": "auth", "signature": stale})
        self.assertEqual((await ws2.receive_json())["reason"], "bad signature")

    async def test_revoking_cuts_the_active_connection_and_blocks_the_phone(self):
        device_id = await self._pair()
        ws = await self._connect_as(device_id)
        await ws.receive_json()  # ready
        admin = TestClient(TestServer(create_admin_app(self.app)))
        await admin.start_server()
        self.addAsyncCleanup(admin.close)
        response = await admin.post(f"/api/devices/{device_id}/revoke", headers={CSRF_HEADER: "1"})
        self.assertEqual(response.status, 200)
        message = await ws.receive()
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 4403)
        self.assertIsNone(self.state.registry.get(device_id))
        again = await self.client.ws_connect("/ws")
        await again.send_json({"type": "hello", "device": device_id, "sampleRate": 48000})
        self.assertEqual((await again.receive_json())["reason"], "unknown device")

    async def test_the_browser_code_still_works_next_to_paired_phones(self):
        await self._pair()
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "code": CODE, "sampleRate": 48000})
        self.assertEqual((await ws.receive_json())["type"], "ready")


class AdminPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        info = ServerInfo(name="JMT<PC>", port=8443, pin_hex=PIN, ips=("192.168.1.38",))
        self.app = create_app(CODE, FakeOutput, registry=DeviceRegistry(), enrollment=EnrollmentTokens(), info=info)
        self.state = self.app[STATE]
        self.client = TestClient(TestServer(create_admin_app(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_the_page_shows_a_qr_and_a_fresh_token_each_time(self):
        first = await (await self.client.get("/")).text()
        self.assertIn("<svg", first)
        self.assertIn("Pair a new phone", first)
        token = self.state.enrollment._token
        await self.client.get("/")
        self.assertNotEqual(token, self.state.enrollment._token)
        self.assertFalse(self.state.enrollment.consume(token))

    async def test_the_qr_link_carries_the_token_the_key_pin_and_the_address(self):
        token = self.state.enrollment.create()
        link = pairing_link(token, self.state.info)
        self.assertTrue(link.startswith("phonemic://pair?"))
        for part in (f"t={token}", f"p={PIN}", "h=192.168.1.38%3A8443", "v=1"):
            self.assertIn(part, link)

    async def test_names_are_escaped_in_the_page(self):
        self.state.registry.add("<script>alert(1)</script>", FakePhone().public_key_der)
        page = await (await self.client.get("/")).text()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("JMT<PC>", page)
        self.assertIn("JMT&lt;PC&gt;", page)

    async def test_requests_with_a_foreign_host_header_are_refused(self):
        response = await self.client.get("/", headers={"Host": "evil.example.com"})
        self.assertEqual(response.status, 403)

    async def test_changing_requests_need_the_custom_header(self):
        device = self.state.registry.add("S20 FE", FakePhone().public_key_der)
        self.assertEqual((await self.client.post(f"/api/devices/{device.id}/revoke")).status, 403)
        self.assertIsNotNone(self.state.registry.get(device.id))
        self.assertEqual((await self.client.post(f"/api/devices/{device.id}/revoke", headers={CSRF_HEADER: "1"})).status, 200)
        self.assertEqual((await self.client.post("/api/devices/none/revoke", headers={CSRF_HEADER: "1"})).status, 404)


if __name__ == "__main__":
    unittest.main()
