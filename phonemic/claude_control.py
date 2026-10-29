"""Drives Claude Desktop's dictation and send buttons through Windows UI Automation.

No mouse is moved and no window is brought to the front: the buttons are operated through their accessibility
patterns (a toggle for the microphone, "invoke" for send), the same way a screen reader would.

The logic (which button to look for, what to wait for, what to answer) lives in `ClaudeController` and only talks to a
small `Ui` interface, so it is tested without Claude Desktop. `UiaWindows` is the real implementation. It matches
buttons by their accessible names, which can change with an app update or its language; the names are configurable and
`--check-claude` shows what the PC can see.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger("phonemic")

ACTIONS = ("dictation_start", "dictation_stop", "send")


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    code: str  # ok, already, no_window, no_button, no_text, send_unavailable, not_confirmed, unavailable, ...
    message: str
    dictation: str | None = None  # "idle" or "recording" when known


@dataclass(frozen=True)
class UiNames:
    """Accessible names of the buttons, as regular expressions."""

    dictation_start: re.Pattern[str] = re.compile(r"^(Press and hold to record|Start dictation)$", re.I)
    dictation_stop: re.Pattern[str] = re.compile(r"^Stop dictation$", re.I)
    send: re.Pattern[str] = re.compile(r"^Send( message)?$", re.I)


class UiElement(Protocol):
    name: str

    def toggle(self) -> None: ...

    def invoke(self) -> None: ...


class Ui(Protocol):
    def window_present(self) -> bool: ...

    def find_button(self, pattern: re.Pattern[str]) -> UiElement | None:
        """The first enabled button whose accessible name matches."""

    def prompt_text_length(self) -> int | None:
        """Length of the message box's text without surrounding whitespace; None when the box cannot be found."""


class ControlUnavailable(RuntimeError):
    """The UI Automation machinery is not available (not Windows, comtypes missing...)."""


class ClaudeController:
    """The behaviour of the Dictate and Send buttons, independent of how the UI is reached."""

    def __init__(
        self,
        ui: Ui,
        names: UiNames = UiNames(),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        settle_s: float = 2.0,
        text_wait_s: float = 8.0,
    ) -> None:
        self.ui = ui
        self.names = names
        self._sleep = sleep
        self._clock = clock
        self._settle_s = settle_s
        self._text_wait_s = text_wait_s

    # --- state ---------------------------------------------------------------------------------------------

    def dictation_state(self) -> str | None:
        if self.ui.find_button(self.names.dictation_stop) is not None:
            return "recording"
        if self.ui.find_button(self.names.dictation_start) is not None:
            return "idle"
        return None

    def _wait_until(self, condition: Callable[[], bool], timeout_s: float, step_s: float = 0.15) -> bool:
        deadline = self._clock() + timeout_s
        while True:
            if condition():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(step_s)

    def _has_text(self) -> bool:
        return bool(self.ui.prompt_text_length())

    # --- actions -------------------------------------------------------------------------------------------

    def start_dictation(self) -> ControlResult:
        return self._set_dictation(want="recording")

    def stop_dictation(self) -> ControlResult:
        return self._set_dictation(want="idle")

    def _set_dictation(self, want: str) -> ControlResult:
        if not self.ui.window_present():
            return ControlResult(False, "no_window", "Claude Desktop is not open on the PC.")
        state = self.dictation_state()
        if state is None:
            return ControlResult(False, "no_button", "Could not find Claude Desktop's dictation button.")
        if state == want:
            return ControlResult(True, "already", "Dictation was already " + ("on." if want == "recording" else "off."), state)
        button = self.ui.find_button(self.names.dictation_stop if state == "recording" else self.names.dictation_start)
        if button is None:
            return ControlResult(False, "no_button", "The dictation button disappeared.", state)
        button.toggle()
        if self._wait_until(lambda: self.dictation_state() == want, self._settle_s):
            return ControlResult(True, "ok", "Dictating." if want == "recording" else "Dictation stopped.", want)
        return ControlResult(False, "not_confirmed", "Claude Desktop did not change its dictation state.", self.dictation_state())

    def send(self) -> ControlResult:
        """Send the message box: stops a running dictation first and waits for its text to arrive."""
        if not self.ui.window_present():
            return ControlResult(False, "no_window", "Claude Desktop is not open on the PC.")
        if self.dictation_state() == "recording":
            stopped = self.stop_dictation()
            if not stopped.ok:
                return stopped
            # The transcription shows up a moment after the recording ends.
            self._wait_until(lambda: self._has_text() or self.ui.find_button(self.names.send) is not None, self._text_wait_s, 0.3)
        button = self.ui.find_button(self.names.send)
        if button is None:
            if not self._has_text():
                return ControlResult(False, "no_text", "There is nothing to send yet.", self.dictation_state())
            return ControlResult(False, "send_unavailable", "Claude Desktop shows no send button right now. Is it busy?", self.dictation_state())
        button.invoke()
        return ControlResult(True, "ok", "Sent.", self.dictation_state() or "idle")

    # --- diagnostics ---------------------------------------------------------------------------------------

    def diagnostics(self) -> list[str]:
        """What the PC can see, without pressing anything and without reading the message text."""
        if not self.ui.window_present():
            return ["Claude Desktop window: NOT FOUND (is it open?)"]
        length = self.ui.prompt_text_length()
        return [
            "Claude Desktop window: found",
            f"Dictation: {self.dictation_state() or 'button NOT FOUND'}",
            f"Send button: {'present' if self.ui.find_button(self.names.send) else 'not visible (empty box or busy)'}",
            "Message box: " + ("NOT FOUND" if length is None else f"found, {length} character(s) of text"),
        ]


# --- the real thing: Windows UI Automation --------------------------------------------------------------------

UIA_CONTROL_TYPE_PROPERTY, UIA_NAME_PROPERTY = 30003, 30005
UIA_BUTTON_TYPE, UIA_EDIT_TYPE = 50000, 50004
UIA_INVOKE_PATTERN, UIA_VALUE_PATTERN, UIA_TOGGLE_PATTERN = 10000, 10002, 10015
TREE_SCOPE_DESCENDANTS = 4
MIN_BUTTONS_WHEN_AWAKE = 6  # Chromium builds the page's accessibility tree on demand; until then only the frame shows


def find_claude_window() -> int | None:
    """Handle of Claude Desktop's main window (a visible Electron window of claude.exe), or None."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found: list[tuple[int, str]] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            size = wintypes.DWORD(520)
            image = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                if image.value.lower().endswith("\\claude.exe") and cls.value == "Chrome_WidgetWin_1":
                    found.append((hwnd, title.value))
        finally:
            kernel32.CloseHandle(handle)
        return True

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(callback_type(callback), 0)
    if not found:
        return None
    exact = [hwnd for hwnd, title in found if title == "Claude"]
    return (exact or [found[0][0]])[0]


class _UiaElement:
    def __init__(self, module, element) -> None:
        self._module = module
        self._element = element
        self.name: str = element.CurrentName

    def _pattern(self, pattern_id: int, interface_name: str):
        try:
            return self._element.GetCurrentPattern(pattern_id).QueryInterface(getattr(self._module, interface_name))
        except Exception as exc:  # noqa: BLE001 - comtypes raises several types for an unsupported pattern
            raise ControlUnavailable(f"'{self.name}' does not support {interface_name}") from exc

    def toggle(self) -> None:
        self._pattern(UIA_TOGGLE_PATTERN, "IUIAutomationTogglePattern").Toggle()

    def invoke(self) -> None:
        self._pattern(UIA_INVOKE_PATTERN, "IUIAutomationInvokePattern").Invoke()


class UiaWindows:
    """Reaches Claude Desktop through Windows UI Automation. Create and use it on one COM-initialised thread."""

    def __init__(self, warmup_s: float = 8.0) -> None:
        try:
            import comtypes
            import comtypes.client
        except ImportError as exc:
            raise ControlUnavailable("The 'comtypes' package is not installed.") from exc
        comtypes.CoInitialize()
        self._module = comtypes.client.GetModule("UIAutomationCore.dll")
        self._uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=self._module.IUIAutomation)
        self._warmup_s = warmup_s

    def _root(self):
        hwnd = find_claude_window()
        return None if hwnd is None else self._uia.ElementFromHandle(hwnd)

    def window_present(self) -> bool:
        return find_claude_window() is not None

    def _buttons(self, root):
        condition = self._uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY, UIA_BUTTON_TYPE)
        return root.FindAll(TREE_SCOPE_DESCENDANTS, condition)

    def _awake_buttons(self, root):
        """The window's buttons, giving Chromium time to expose its page the first time it is asked."""
        deadline = time.monotonic() + self._warmup_s
        buttons = self._buttons(root)
        while buttons.Length < MIN_BUTTONS_WHEN_AWAKE and time.monotonic() < deadline:
            time.sleep(0.4)
            buttons = self._buttons(root)
        return buttons

    def find_button(self, pattern: re.Pattern[str]) -> UiElement | None:
        root = self._root()
        if root is None:
            return None
        buttons = self._awake_buttons(root)
        for index in range(buttons.Length):
            element = buttons.GetElement(index)
            if pattern.search(element.CurrentName or "") and element.CurrentIsEnabled:
                return _UiaElement(self._module, element)
        return None

    def prompt_text_length(self) -> int | None:
        root = self._root()
        if root is None:
            return None
        self._awake_buttons(root)
        by_name = self._uia.CreatePropertyCondition(UIA_NAME_PROPERTY, "Prompt")
        by_type = self._uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY, UIA_EDIT_TYPE)
        element = root.FindFirst(TREE_SCOPE_DESCENDANTS, self._uia.CreateAndCondition(by_name, by_type))
        if element is None:
            return None
        try:
            pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN).QueryInterface(self._module.IUIAutomationValuePattern)
            return len((pattern.CurrentValue or "").strip())
        except Exception:  # noqa: BLE001
            return None


class ClaudeRemote:
    """Async front for the server: runs the blocking UI Automation calls one at a time on a dedicated thread."""

    def __init__(self, factory: Callable[[], ClaudeController] | None = None) -> None:
        self._factory = factory or (lambda: ClaudeController(UiaWindows()))
        self._controller: ClaudeController | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phonemic-ui")

    def _controller_on_worker(self) -> ClaudeController:
        if self._controller is None:  # built on the worker thread so that its COM objects live there
            self._controller = self._factory()
        return self._controller

    def _run_blocking(self, action: str) -> ControlResult:
        try:
            controller = self._controller_on_worker()
            return {"dictation_start": controller.start_dictation, "dictation_stop": controller.stop_dictation, "send": controller.send}[action]()
        except ControlUnavailable as exc:
            return ControlResult(False, "unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 - a UI Automation failure must never break the audio session
            log.exception("Remote control %s failed", action)
            return ControlResult(False, "error", f"Could not operate Claude Desktop: {exc}")

    async def run(self, action: str) -> ControlResult:
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._run_blocking, action)

    def diagnostics(self) -> list[str]:
        """Blocking; for the --check-claude command."""
        try:
            return self._controller_on_worker().diagnostics()
        except ControlUnavailable as exc:
            return [f"Not available: {exc}"]
