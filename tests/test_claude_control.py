import asyncio
import re
import unittest

from phonemic.claude_control import ClaudeController, ClaudeRemote, ControlResult, ControlUnavailable, UiNames


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeButton:
    def __init__(self, ui: "FakeUi", name: str) -> None:
        self.ui, self.name = ui, name

    def toggle(self) -> None:
        self.ui.toggles += 1
        if self.ui.ignore_toggle:
            return
        if self.name in ("Press and hold to record", "Stop dictation"):
            self.ui.recording = not self.ui.recording
            if not self.ui.recording and self.ui.transcript:
                self.ui.arrives_at = self.ui.clock.now + self.ui.transcript_delay_s

    def invoke(self) -> None:
        self.ui.sent += 1
        self.ui.text = 0


class FakeUi:
    """A stand-in for Claude Desktop: a dictation toggle, a message box and a send button that needs text."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.window = True
        self.recording = False
        self.text = 0
        self.busy = False  # Claude is answering: a Stop button instead of Send
        self.has_dictation_button = True
        self.ignore_toggle = False
        self.transcript = 12  # characters that show up after a dictation
        self.transcript_delay_s = 0.0  # ...this many seconds after it stops
        self.arrives_at: float | None = None
        self.toggles = 0
        self.sent = 0

    def _tick(self) -> None:
        if self.arrives_at is not None and self.clock.now >= self.arrives_at:
            self.text += self.transcript
            self.arrives_at = None

    def window_present(self) -> bool:
        return self.window

    def find_button(self, pattern: re.Pattern[str]):
        if not self.window:
            return None
        self._tick()
        names = []
        if self.has_dictation_button:
            names.append("Stop dictation" if self.recording else "Press and hold to record")
        if self.text and not self.busy:
            names.append("Send")
        names += ["Add", "Stop"] if self.busy else ["Add", "Send feedback"]
        for name in names:
            if pattern.search(name):
                return FakeButton(self, name)
        return None

    def prompt_text_length(self) -> int | None:
        if not self.window:
            return None
        self._tick()
        return self.text


class ClaudeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.ui = FakeUi(self.clock)
        self.controller = ClaudeController(self.ui, sleep=self.clock.sleep, clock=self.clock.time)

    def test_start_dictation_turns_the_microphone_on(self):
        result = self.controller.start_dictation()
        self.assertEqual((result.ok, result.code, result.dictation), (True, "ok", "recording"))
        self.assertTrue(self.ui.recording)

    def test_starting_twice_does_not_toggle_it_back_off(self):
        self.controller.start_dictation()
        result = self.controller.start_dictation()
        self.assertEqual((result.ok, result.code), (True, "already"))
        self.assertEqual(self.ui.toggles, 1)

    def test_stop_dictation_turns_it_off_and_stopping_when_off_is_a_no_op(self):
        self.controller.start_dictation()
        self.assertEqual(self.controller.stop_dictation().dictation, "idle")
        again = self.controller.stop_dictation()
        self.assertEqual((again.ok, again.code), (True, "already"))
        self.assertEqual(self.ui.toggles, 2)

    def test_everything_reports_a_missing_window(self):
        self.ui.window = False
        for action in (self.controller.start_dictation, self.controller.stop_dictation, self.controller.send):
            self.assertEqual(action().code, "no_window")

    def test_a_missing_dictation_button_is_reported(self):
        self.ui.has_dictation_button = False
        self.assertEqual(self.controller.start_dictation().code, "no_button")

    def test_a_toggle_that_does_nothing_is_reported_instead_of_trusted(self):
        self.ui.ignore_toggle = True
        result = self.controller.start_dictation()
        self.assertEqual((result.ok, result.code), (False, "not_confirmed"))
        self.assertGreaterEqual(self.clock.now, 2.0)  # it waited for the state to change before giving up

    def test_send_presses_the_send_button_when_there_is_text(self):
        self.ui.text = 30
        result = self.controller.send()
        self.assertEqual((result.ok, result.code), (True, "ok"))
        self.assertEqual(self.ui.sent, 1)

    def test_send_while_dictating_stops_waits_for_the_transcript_and_sends(self):
        self.controller.start_dictation()
        self.ui.transcript_delay_s = 1.2  # the text arrives a little after the recording stops
        result = self.controller.send()
        self.assertEqual((result.ok, result.code), (True, "ok"))
        self.assertFalse(self.ui.recording)
        self.assertEqual(self.ui.sent, 1)

    def test_send_does_not_wait_forever_for_a_transcript_that_is_too_slow(self):
        self.controller.start_dictation()
        self.ui.transcript_delay_s = 60.0
        result = self.controller.send()
        self.assertEqual(result.code, "no_text")
        self.assertLess(self.clock.now, 15.0)

    def test_send_with_nothing_to_send_says_so_and_sends_nothing(self):
        result = self.controller.send()
        self.assertEqual((result.ok, result.code), (False, "no_text"))
        self.assertEqual(self.ui.sent, 0)

    def test_send_after_a_silent_dictation_gives_up_with_no_text(self):
        self.ui.transcript = 0
        self.controller.start_dictation()
        result = self.controller.send()
        self.assertEqual(result.code, "no_text")
        self.assertEqual(self.ui.sent, 0)
        self.assertFalse(self.ui.recording)  # it did stop the dictation

    def test_send_while_claude_is_busy_explains_why(self):
        self.ui.text = 30
        self.ui.busy = True
        result = self.controller.send()
        self.assertEqual((result.ok, result.code), (False, "send_unavailable"))
        self.assertEqual(self.ui.sent, 0)

    def test_send_feedback_is_not_mistaken_for_the_send_button(self):
        self.assertIsNone(self.ui.find_button(UiNames().send))

    def test_button_names_are_configurable(self):
        ui = FakeUi(self.clock)
        names = UiNames(dictation_start=re.compile("^Press and hold to record$"), dictation_stop=re.compile("^Nope$"))
        controller = ClaudeController(ui, names=names, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(controller.dictation_state(), "idle")
        ui.recording = True  # now the button is called "Stop dictation", which these names do not know
        self.assertIsNone(controller.dictation_state())

    def test_diagnostics_describe_the_ui_without_leaking_the_message(self):
        self.ui.text = 7
        report = "\n".join(self.controller.diagnostics())
        self.assertIn("found", report)
        self.assertIn("7 character(s)", report)
        self.assertIn("Dictation: idle", report)
        self.ui.window = False
        self.assertIn("NOT FOUND", self.controller.diagnostics()[0])


class ClaudeRemoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_the_matching_action_on_a_worker_thread(self):
        clock = FakeClock()
        ui = FakeUi(clock)
        remote = ClaudeRemote(lambda: ClaudeController(ui, sleep=clock.sleep, clock=clock.time))
        result = await remote.run("dictation_start")
        self.assertEqual((result.ok, result.dictation), (True, "recording"))
        self.assertTrue(ui.recording)

    async def test_an_unavailable_backend_becomes_a_result_not_an_exception(self):
        def broken():
            raise ControlUnavailable("comtypes is missing")

        result = await ClaudeRemote(broken).run("send")
        self.assertEqual((result.ok, result.code), (False, "unavailable"))
        self.assertIn("comtypes", result.message)

    async def test_any_other_failure_is_contained(self):
        def broken():
            raise RuntimeError("boom")

        with self.assertLogs("phonemic", level="ERROR"):
            result = await ClaudeRemote(broken).run("send")
        self.assertEqual((result.ok, result.code), (False, "error"))

    async def test_calls_are_serialised(self):
        clock = FakeClock()
        ui = FakeUi(clock)
        remote = ClaudeRemote(lambda: ClaudeController(ui, sleep=clock.sleep, clock=clock.time))
        first, second = await asyncio.gather(remote.run("dictation_start"), remote.run("dictation_start"))
        self.assertEqual(sorted([first.code, second.code]), ["already", "ok"])
        self.assertEqual(ui.toggles, 1)


if __name__ == "__main__":
    unittest.main()
