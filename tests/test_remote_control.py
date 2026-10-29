"""Remote control over real sockets: permissions, safeguards, and that the audio never waits for a button press."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from phonemic.admin import CSRF_HEADER, create_admin_app
from phonemic.claude_control import ControlResult
from phonemic.devices import DeviceRegistry, EnrollmentTokens
from phonemic.pcm_queue import PcmQueue
from phonemic.server import STATE, ServerInfo, create_app
from tests.test_devices import FakePhone

CODE = "123456"


class FakeOutput:
    def __init__(self, sample_rate: int) -> None:
        self.queue = PcmQueue(sample_rate, prebuffer_ms=0)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeRemote:
    """Records what the server asked Claude Desktop to do; can be told to take its time."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.results: dict[str, ControlResult] = {}

    async def run(self, action: str) -> ControlResult:
        self.calls.append(action)
        if self.gate is not None:
            await self.gate.wait()
        return self.results.get(action, ControlResult(True, "ok", "done", "recording" if action == "dictation_start" else "idle"))


class RemoteControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch("phonemic.server.AUTH_FAIL_DELAY_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = [1000.0]
        self.remote = FakeRemote()
        self.app = create_app(
            CODE, FakeOutput, registry=DeviceRegistry(), enrollment=EnrollmentTokens(),
            info=ServerInfo(name="MY-PC"), controller=self.remote, clock=lambda: self.now[0],
        )
        self.state = self.app[STATE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.admin = TestClient(TestServer(create_admin_app(self.app)))
        await self.admin.start_server()
        self.addAsyncCleanup(self.admin.close)
        self.phone = FakePhone()
        device = self.state.registry.add("Pixel 6", self.phone.public_key_der)
        self.device_id = device.id

    async def _connect(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "device": self.device_id, "sampleRate": 48000})
        challenge = await ws.receive_json()
        await ws.send_json({"type": "auth", "signature": base64.b64encode(self.phone.sign(challenge["nonce"])).decode()})
        ready = await ws.receive_json()
        self.assertEqual(ready["type"], "ready")
        return ws, ready

    async def _allow(self, dictation: bool, send: bool):
        return await self.admin.post(
            f"/api/devices/{self.device_id}/permissions", headers={CSRF_HEADER: "1"}, json={"dictation": dictation, "send": send}
        )

    async def _control(self, ws, action: str) -> dict:
        await ws.send_json({"type": "control", "action": action})
        return await asyncio.wait_for(ws.receive_json(), 3)

    # --- permissions ---------------------------------------------------------------------------------------

    async def test_a_new_phone_may_do_nothing_but_be_a_microphone(self):
        ws, ready = await self._connect()
        self.assertEqual(ready["capabilities"], {"dictation": False, "send": False})
        reply = await self._control(ws, "dictation_start")
        self.assertEqual((reply["ok"], reply["code"]), (False, "not_allowed"))
        self.assertEqual(self.remote.calls, [])

    async def test_switching_a_permission_on_reaches_the_connected_phone_at_once(self):
        ws, _ = await self._connect()
        response = await self._allow(dictation=True, send=False)
        self.assertEqual(await response.json(), {"dictation": True, "send": False})
        pushed = await asyncio.wait_for(ws.receive_json(), 3)
        self.assertEqual(pushed, {"type": "capabilities", "capabilities": {"dictation": True, "send": False}})
        reply = await self._control(ws, "dictation_start")
        self.assertEqual((reply["ok"], reply["dictation"]), (True, "recording"))
        self.assertEqual(self.remote.calls, ["dictation_start"])

    async def test_sending_implies_dictating(self):
        response = await self._allow(dictation=False, send=True)
        self.assertEqual(await response.json(), {"dictation": True, "send": True})

    async def test_dictation_alone_does_not_allow_sending(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        await self._control(ws, "dictation_start")
        reply = await self._control(ws, "send")
        self.assertEqual((reply["ok"], reply["code"]), (False, "not_allowed"))
        self.assertEqual(self.remote.calls, ["dictation_start"])

    async def test_revoking_the_permission_stops_it_working_immediately(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        self.assertTrue((await self._control(ws, "dictation_start"))["ok"])
        await self._allow(dictation=False, send=False)
        await ws.receive_json()  # the capabilities push
        self.now[0] += 5
        self.assertEqual((await self._control(ws, "dictation_stop"))["code"], "not_allowed")

    # --- safeguards ----------------------------------------------------------------------------------------

    async def test_send_needs_this_phone_to_have_dictated_first_and_only_once_per_dictation(self):
        await self._allow(dictation=True, send=True)
        ws, _ = await self._connect()
        self.assertEqual((await self._control(ws, "send"))["code"], "nothing_dictated")
        self.assertEqual(self.remote.calls, [])
        self.now[0] += 1
        self.assertTrue((await self._control(ws, "dictation_start"))["ok"])
        self.now[0] += 1
        self.assertTrue((await self._control(ws, "send"))["ok"])
        self.now[0] += 1
        self.assertEqual((await self._control(ws, "send"))["code"], "nothing_dictated")
        self.assertEqual(self.remote.calls, ["dictation_start", "send"])

    async def test_a_failed_send_keeps_the_dictation_so_it_can_be_retried(self):
        await self._allow(dictation=True, send=True)
        ws, _ = await self._connect()
        await self._control(ws, "dictation_start")
        self.remote.results["send"] = ControlResult(False, "send_unavailable", "busy")
        self.now[0] += 1
        self.assertEqual((await self._control(ws, "send"))["code"], "send_unavailable")
        self.remote.results.pop("send")
        self.now[0] += 1
        self.assertTrue((await self._control(ws, "send"))["ok"])

    async def test_actions_are_rate_limited(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        self.assertTrue((await self._control(ws, "dictation_start"))["ok"])
        too_soon = await self._control(ws, "dictation_stop")  # same instant
        self.assertEqual(too_soon["code"], "rate_limited")
        self.now[0] += 0.6
        self.assertTrue((await self._control(ws, "dictation_stop"))["ok"])

    async def test_there_is_also_a_ceiling_per_minute(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        codes = []
        for _ in range(25):
            self.now[0] += 1  # one a second: fine for the interval, too many for a minute
            codes.append((await self._control(ws, "dictation_stop"))["code"])
        self.assertEqual(codes.count("ok"), 20)
        self.assertEqual(codes[-1], "rate_limited")

    async def test_an_action_still_running_blocks_the_next_one_but_the_audio_keeps_flowing(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        self.remote.gate = asyncio.Event()
        await ws.send_json({"type": "control", "action": "dictation_start"})  # blocks inside the fake controller
        await asyncio.sleep(0.05)
        await ws.send_bytes(np.arange(960, dtype="<i2").tobytes())  # audio arrives while the button is being pressed
        for _ in range(50):
            if self.state.active.output.queue.buffered_samples == 960:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.state.active.output.queue.buffered_samples, 960)
        self.now[0] += 1
        busy = await self._control(ws, "dictation_stop")
        self.assertEqual(busy["code"], "in_progress")
        self.remote.gate.set()
        done = await asyncio.wait_for(ws.receive_json(), 3)
        self.assertEqual((done["action"], done["ok"]), ("dictation_start", True))

    async def test_unknown_actions_and_missing_controller_are_answered(self):
        await self._allow(dictation=True, send=True)
        ws, _ = await self._connect()
        self.assertEqual((await self._control(ws, "format_disk"))["code"], "bad_action")
        self.now[0] += 1
        self.state.controller = None
        self.assertEqual((await self._control(ws, "dictation_start"))["code"], "unavailable")

    async def test_browser_sessions_cannot_control_anything(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "code": CODE, "sampleRate": 48000})
        ready = await ws.receive_json()
        self.assertEqual(ready["capabilities"], {"dictation": False, "send": False})
        await ws.send_json({"type": "control", "action": "send"})
        await asyncio.sleep(0.2)
        self.assertEqual(self.remote.calls, [])

    async def test_garbage_text_messages_are_ignored(self):
        await self._allow(dictation=True, send=False)
        ws, _ = await self._connect()
        for raw in ("not json", json.dumps([1, 2]), json.dumps({"type": "other"}), json.dumps({"type": "control", "action": 5})):
            await ws.send_str(raw)
        reply = await asyncio.wait_for(ws.receive_json(), 3)  # only the non-string action is answered
        self.assertEqual(reply["code"], "bad_action")
        self.assertEqual(self.remote.calls, [])


class PermissionsAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(CODE, FakeOutput, registry=DeviceRegistry(), enrollment=EnrollmentTokens(), info=ServerInfo(name="MY-PC"))
        self.state = self.app[STATE]
        self.admin = TestClient(TestServer(create_admin_app(self.app)))
        await self.admin.start_server()
        self.addAsyncCleanup(self.admin.close)
        self.device = self.state.registry.add("Pixel 6", FakePhone().public_key_der)

    async def test_the_page_shows_the_checkboxes_with_their_state(self):
        page = await (await self.admin.get("/")).text()
        self.assertIn('data-kind="dictation"', page)
        self.assertIn("Allow sending", page)
        self.assertNotIn(" checked", page.split('id="devices"')[1].split("</tbody>")[0])
        self.state.registry.set_permissions(self.device.id, dictation=True, send=False)
        row = (await (await self.admin.get("/")).text()).split('id="devices"')[1].split("</tbody>")[0]
        self.assertEqual(row.count(" checked"), 1)

    async def test_the_endpoint_validates_and_is_protected(self):
        url = f"/api/devices/{self.device.id}/permissions"
        body = {"dictation": True, "send": False}
        self.assertEqual((await self.admin.post(url, json=body)).status, 403)  # no CSRF header
        for bad in ({}, {"dictation": "yes", "send": False}, {"dictation": True}, {"dictation": 1, "send": 0}):
            self.assertEqual((await self.admin.post(url, headers={CSRF_HEADER: "1"}, json=bad)).status, 400, bad)
        self.assertEqual((await self.admin.post("/api/devices/none/permissions", headers={CSRF_HEADER: "1"}, json=body)).status, 404)
        self.assertEqual(self.state.registry.get(self.device.id).permissions, {"dictation": False, "send": False})
        self.assertEqual((await self.admin.post(url, headers={CSRF_HEADER: "1"}, json=body)).status, 200)
        self.assertEqual(self.state.registry.get(self.device.id).permissions, {"dictation": True, "send": False})

    async def test_permissions_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.json"
            registry = DeviceRegistry(path)
            device = registry.add("Pixel 6", FakePhone().public_key_der)
            registry.set_permissions(device.id, dictation=True, send=True)
            self.assertEqual(DeviceRegistry(path).get(device.id).permissions, {"dictation": True, "send": True})

    async def test_an_old_devices_file_without_permissions_loads_with_everything_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.json"
            registry = DeviceRegistry(path)
            device = registry.add("Pixel 6", FakePhone().public_key_der)
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data["devices"]:
                del item["permissions"]
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(DeviceRegistry(path).get(device.id).permissions, {"dictation": False, "send": False})

    async def test_a_corrupt_file_cannot_grant_sending_without_dictation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.json"
            registry = DeviceRegistry(path)
            device = registry.add("Pixel 6", FakePhone().public_key_der)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["devices"][0]["permissions"] = {"dictation": False, "send": True, "root": True}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(DeviceRegistry(path).get(device.id).permissions, {"dictation": True, "send": True})


if __name__ == "__main__":
    unittest.main()
