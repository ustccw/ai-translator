"""Read the active window and the X11 selection, and paste with the keyboard.

Clipboard owners must outlive this process: xclip is started in its own
session so a later restore is still served after we exit.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import time

from Xlib import X, display as xdisplay


class X11Error(RuntimeError):
    pass


def _run(args: list[str], data: bytes | None = None, timeout: float = 2) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise X11Error(f"找不到命令 {args[0]}") from exc


def active_window() -> tuple[int, str, str]:
    """Return (window id, instance, class). Zeros and empty strings when unreadable."""
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0, "", ""
    try:
        root = disp.screen().root
        prop = root.get_full_property(disp.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        if prop is None or not prop.value:
            return 0, "", ""
        window_id = int(prop.value[0])
        window = disp.create_resource_object("window", window_id)
        wm_class = window.get_full_property(disp.intern_atom("WM_CLASS"), X.AnyPropertyType)
        if wm_class is None or not wm_class.value:
            return window_id, "", ""
        parts = [part.decode("utf-8", "replace") for part in bytes(wm_class.value).split(b"\x00") if part]
        instance = parts[0] if parts else ""
        klass = parts[1] if len(parts) > 1 else instance
        return window_id, instance, klass
    except Exception:
        return 0, "", ""
    finally:
        disp.close()


def active_wm_class() -> tuple[str, str]:
    """Return (instance, class). Both are empty when the window cannot be read."""
    _window_id, instance, klass = active_window()
    return instance, klass


def focus_window(window_id: int) -> bool:
    """Raise a window and wait until it is active. False when it cannot be focused."""
    if window_id <= 0:
        return False
    try:
        result = _run(["xdotool", "windowactivate", "--sync", str(window_id)], timeout=1.5)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def pointer_position() -> tuple[int, int]:
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0, 0
    try:
        pointer = disp.screen().root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)
    except Exception:
        return 0, 0
    finally:
        disp.close()


def release_modifiers() -> None:
    """Drop the hotkey modifiers so they are not still held for Ctrl+C or Ctrl+V."""
    _run([
        "xdotool", "keyup",
        "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Shift_L", "Shift_R", "Super_L", "Super_R",
    ])


def read_text_selection(selection: str) -> tuple[bool, str]:
    """Return (available, text). available is False when the selection has no text."""
    for target in ("UTF8_STRING", "STRING"):
        result = _run(["xclip", "-selection", selection, "-o", "-t", target])
        if result.returncode == 0:
            text = result.stdout.decode("utf-8", "replace").replace("\x00", "")
            return True, text
    return False, ""


def write_clipboard(text: str) -> None:
    proc = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    proc.wait(timeout=2)


def send_key(combo: str) -> None:
    _run(["xdotool", "key", "--clearmodifiers", combo])


class ClipboardBackup:
    """Remember the text clipboard so a copy or a paste can put it back.

    Non-text contents (an image, for example) cannot be restored. That only
    matters on the Ctrl+C fallback, when PRIMARY was empty.
    """

    def __init__(self) -> None:
        self.had_text = False
        self.original = ""
        self.dirty = False
        self._restored = False

    def save(self) -> ClipboardBackup:
        self.had_text, self.original = read_text_selection("clipboard")
        return self

    def mark_dirty(self) -> None:
        if not self.dirty:
            self.dirty = True
            atexit.register(self.restore)

    def keep(self) -> None:
        """Leave the clipboard as it is. A later restore must not replace it."""
        self._restored = True

    def restore(self) -> None:
        if self._restored or not self.dirty:
            return
        self._restored = True
        if self.had_text:
            write_clipboard(self.original)
        else:
            write_clipboard("")


def _copy_via_keyboard(backup: ClipboardBackup, copy_wait_ms: int) -> str | None:
    """Ask the focused editable widget for its selection.

    PRIMARY is not used here. It can still hold an older selection from
    another window, and a later paste would replace the real selection
    with a rewrite of that stale text. A sentinel makes "clipboard did
    not change" mean the copy failed, even when both texts are identical.
    """
    token = f"\u2063ai-translator-{os.getpid()}-{time.time_ns()}\u2063"
    backup.mark_dirty()
    write_clipboard(token)
    send_key("ctrl+c")
    deadline = time.monotonic() + (copy_wait_ms / 1000)
    while True:
        ok, current = read_text_selection("clipboard")
        if ok and current != token and current.strip():
            return current
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    backup.restore()
    return None


def capture_selection(surface: str, copy_wait_ms: int) -> tuple[str | None, ClipboardBackup]:
    """Editable windows copy with Ctrl+C. Read-only windows only read PRIMARY."""
    backup = ClipboardBackup().save()
    if surface == "editable":
        return _copy_via_keyboard(backup, copy_wait_ms), backup
    available, primary = read_text_selection("primary")
    if available and primary.strip():
        return primary, backup
    return None, backup


def paste_text(text: str, backup: ClipboardBackup, restore_ms: int, window_id: int = 0) -> None:
    """Put text on the clipboard and, when the source window can be focused, paste there.

    The chosen text stays on the clipboard. Ctrl+V is sent only after that
    window is active, so a click in another window does not receive the paste.
    """
    backup.keep()
    write_clipboard(text)
    time.sleep(0.05)
    if not focus_window(window_id):
        return
    time.sleep(0.05)
    send_key("ctrl+v")
    time.sleep(max(restore_ms, 0) / 1000)
