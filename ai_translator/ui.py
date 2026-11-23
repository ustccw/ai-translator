"""A focusable popup. Clicking another window gives that window the keyboard."""

from __future__ import annotations

import math
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from ai_translator import __version__
from ai_translator.config import Config
from ai_translator.prompt import complete
from ai_translator.x11util import ClipboardBackup, paste_text, pointer_position, write_clipboard

TITLES = {
    "translate": "互译",
    "polish": "润色",
}


def _heading(label: str) -> str:
    return f"ai-translator (v{__version__}) — {label}"


def _init_gtk() -> None:
    result = Gtk.init_check([])
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if not ok:
        raise RuntimeError("无法打开图形界面")


class Popup:
    def __init__(self, cfg: Config, backup: ClipboardBackup) -> None:
        _init_gtk()
        self.cfg = cfg
        self.backup = backup
        self.closed = False
        self.phase = "notice"
        self.surface = "readonly"
        self.action = "translate"
        self.source = ""
        self.count = 1
        self.candidates: list[str] = []
        self.source_window = 0
        self.pointer = pointer_position()
        self.user_moved = False
        self._placed = False
        self._lifting = False
        self._drag_offset: tuple[float, float] | None = None
        self._pointer_grabbed = False
        self._shaped = (0, 0)

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_name("ai-translator")
        self.window.set_title(f"ai-translator (v{__version__})")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(True)
        self.window.set_focus_on_map(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.window.add_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.window.connect("key-press-event", self._on_key)
        self.window.connect("button-press-event", self._on_button)
        self.window.connect("button-release-event", self._on_drag_release)
        self.window.connect("motion-notify-event", self._on_drag_motion)
        self.window.connect("size-allocate", self._on_size_allocate)
        self.window.connect("destroy", self._on_destroy)

        self.drag_bar = Gtk.EventBox()
        self.drag_bar.get_style_context().add_class("drag-bar")
        self.drag_bar.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.drag_bar.connect("button-press-event", self._on_drag_press)
        self.drag_bar.connect("realize", lambda _widget: self._install_drag_cursor())
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_border_width(8)
        self.title = Gtk.Label(xalign=0)
        self.title.set_hexpand(True)
        self.title.set_halign(Gtk.Align.START)
        self.title.set_valign(Gtk.Align.CENTER)
        self.title.get_style_context().add_class("title")
        self.close_button = Gtk.Button(label="×")
        self.close_button.set_relief(Gtk.ReliefStyle.NONE)
        self.close_button.set_can_focus(False)
        self.close_button.set_valign(Gtk.Align.CENTER)
        self.close_button.get_style_context().add_class("close")
        self.close_button.connect("clicked", lambda _button: self._finish(restore=True))
        header.pack_start(self.title, True, True, 0)
        header.pack_end(self.close_button, False, False, 0)
        self.drag_bar.add(header)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.content.set_border_width(14)
        self.content.set_size_request(cfg.width, -1)
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.get_style_context().add_class("hint")
        self.subtitle.set_no_show_all(True)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.body.get_style_context().add_class("candidates")
        self.hint = Gtk.Label(xalign=0)
        self.hint.get_style_context().add_class("hint")
        self.hint.set_line_wrap(True)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_width(True)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.add(self.body)
        self.content.pack_start(self.subtitle, False, False, 0)
        self.content.pack_start(self.scroll, True, True, 0)
        self.content.pack_start(self.hint, False, False, 0)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.pack_start(self.drag_bar, False, False, 0)
        shell.pack_start(self.content, True, True, 0)
        self.window.add(shell)
        self._apply_style()

    def _apply_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            #ai-translator {
                background-color: #222222;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
            }
            #ai-translator label { color: #dddddd; }
            #ai-translator .title { font-weight: bold; }
            #ai-translator .hint { color: #999999; }
            #ai-translator .drag-bar {
                background-color: #1a1a1a;
                border-bottom: 2px solid #F9690E;
            }
            #ai-translator button {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 6px 4px;
                color: #dddddd;
            }
            #ai-translator button:hover { background-color: #2e2e2e; }
            #ai-translator button.close {
                padding: 0 6px;
                min-width: 24px;
                min-height: 24px;
            }
            #ai-translator .candidates .candidate:not(:first-child) {
                border-top: 1px solid #3a3a3a;
                margin-top: 8px;
                padding-top: 8px;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        desc = Pango.FontDescription.from_string(self.cfg.font)
        self._font = desc

    def _font_on(self, widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Label):
            widget.override_font(self._font)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._font_on(child)

    def _clear_body(self) -> None:
        for child in list(self.body.get_children()):
            self.body.remove(child)

    def _wrapped(self, text: str, hint: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(max(24, self.cfg.width // 11))
        label.set_selectable(False)
        if hint:
            label.get_style_context().add_class("hint")
        return label

    def _show(self) -> None:
        self._limit_height()
        self.window.show_all()
        if not self.subtitle.get_text():
            self.subtitle.hide()
        self._font_on(self.window)
        self._move_on_screen()
        self._install_drag_cursor()
        self._focus()
        GLib.idle_add(self._focus)

    def _focus(self) -> bool:
        """Ask the window manager for the keyboard. Another window can take it back."""
        if self.closed:
            return False
        gdk_window = self.window.get_window()
        if gdk_window is None:
            return False
        timestamp = Gdk.CURRENT_TIME
        try:
            from gi.repository import GdkX11

            timestamp = GdkX11.x11_get_server_time(gdk_window)
        except Exception:
            timestamp = Gdk.CURRENT_TIME
        self.window.present_with_time(timestamp)
        return False

    def _on_destroy(self, _widget) -> None:
        self.closed = True
        self._end_drag()

    def _finish(self, restore: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self._end_drag()
        if restore:
            self.backup.restore()
        self.window.destroy()
        if Gtk.main_level() > 0:
            Gtk.main_quit()

    def _on_button(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        if self._drag_offset is not None:
            return True
        origin = self.window.get_window()
        if origin is None:
            return False
        x, y = self.window.get_position()
        width, height = self.window.get_size()
        inside = x <= event.x_root <= x + width and y <= event.y_root <= y + height
        if not inside:
            self._finish(restore=True)
            return True
        return False

    def _install_drag_cursor(self) -> None:
        gdk_window = self.drag_bar.get_window()
        display = self.window.get_display()
        if gdk_window is None or display is None:
            return
        cursor = Gdk.Cursor.new_from_name(display, "move")
        if cursor is None:
            cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.FLEUR)
        gdk_window.set_cursor(cursor)

    def _on_drag_press(self, _widget, event) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        origin_x, origin_y = self.window.get_position()
        self._drag_offset = (event.x_root - origin_x, event.y_root - origin_y)
        # Pointer only, and only while the bar is held. owner_events stays false
        # so the release still arrives after the pointer leaves the bar.
        if not self._grab_pointer(event.time):
            self._drag_offset = None
        return True

    def _grab_pointer(self, time_ms: int) -> bool:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        pointer = seat.get_pointer() if seat is not None else None
        gdk_window = self.window.get_window()
        if pointer is None or gdk_window is None:
            return False
        mask = Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK
        status = pointer.grab(
            gdk_window,
            Gdk.GrabOwnership.APPLICATION,
            False,
            mask,
            None,
            time_ms,
        )
        if status != Gdk.GrabStatus.SUCCESS:
            return False
        self._pointer_grabbed = True
        return True

    def _on_drag_motion(self, _widget, event) -> bool:
        if self._drag_offset is None:
            return False
        origin_x, origin_y = self._drag_offset
        next_x = int(round(event.x_root - origin_x))
        next_y = int(round(event.y_root - origin_y))
        if (next_x, next_y) != self.window.get_position():
            self.user_moved = True
            self.window.move(next_x, next_y)
        return True

    def _on_drag_release(self, _widget, event) -> bool:
        if self._drag_offset is None or event.button != 1:
            return False
        self._end_drag(event.time)
        return True

    def _end_drag(self, time_ms: int = Gdk.CURRENT_TIME) -> None:
        grabbed = self._pointer_grabbed
        self._drag_offset = None
        self._pointer_grabbed = False
        if not grabbed:
            return
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        pointer = seat.get_pointer() if seat is not None else None
        if pointer is not None:
            pointer.ungrab(time_ms)

    def _on_size_allocate(self, _widget, allocation) -> None:
        self._round_corners(allocation.width, allocation.height)
        self._lift_if_flush(allocation.height)

    def _round_corners(self, width: int, height: int) -> None:
        # An override-redirect window has no frame, so CSS radius does not
        # clip it. The shape mask is what actually rounds the corners.
        if (width, height) == self._shaped or width < 4 or height < 4:
            return
        gdk_window = self.window.get_window()
        if gdk_window is None:
            return
        try:
            import cairo

            radius = min(6, width / 2, height / 2)
            surface = cairo.ImageSurface(cairo.FORMAT_A8, width, height)
            context = cairo.Context(surface)
            context.new_sub_path()
            context.arc(width - radius, radius, radius, -math.pi / 2, 0)
            context.arc(width - radius, height - radius, radius, 0, math.pi / 2)
            context.arc(radius, height - radius, radius, math.pi / 2, math.pi)
            context.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
            context.close_path()
            context.set_source_rgba(0, 0, 0, 1)
            context.fill()
            region = Gdk.cairo_region_create_from_surface(surface)
            gdk_window.shape_combine_region(region, 0, 0)
        except Exception:
            return
        self._shaped = (width, height)

    def _on_key(self, _widget, event) -> bool:
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self._finish(restore=True)
            return True
        if self.phase == "notice" and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._finish(restore=True)
            return True
        if self.phase != "results":
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and len(self.candidates) == 1:
            self._choose(0)
            return True
        number = _number_key(key)
        if number is not None:
            self._choose(number - 1)
            return True
        return True

    def _choose(self, index: int) -> None:
        if self.closed or not (0 <= index < len(self.candidates)):
            return
        text = self.candidates[index]
        self.closed = True
        self._end_drag()
        self.window.hide()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            if self.surface == "editable":
                paste_text(text, self.backup, self.cfg.clipboard_restore_ms, self.source_window)
            else:
                self.backup.keep()
                write_clipboard(text)
        finally:
            self.window.destroy()
            if Gtk.main_level() > 0:
                Gtk.main_quit()

    def present_notice(self, title: str, message: str) -> None:
        self.phase = "notice"
        self.window.set_title(_heading(title))
        self.title.set_text(_heading(title))
        self._clear_body()
        self.body.pack_start(self._wrapped(message), False, False, 0)
        self.hint.set_text("Esc 关闭")
        self._show()
        Gtk.main()

    def present_request(
        self,
        action: str,
        surface: str,
        source: str,
        count: int,
        window_id: int,
    ) -> None:
        self.action = action
        self.surface = surface
        self.source = source
        self.count = count
        self.source_window = window_id
        heading = _heading(TITLES.get(action, "互译"))
        self.window.set_title(heading)
        self.title.set_text(heading)
        self.phase = "loading"
        self._clear_body()
        self.body.pack_start(self._wrapped("处理中…"), False, False, 0)
        self.hint.set_text("Esc 取消")
        self._show()
        self._start_thread()
        Gtk.main()

    def _start_thread(self) -> None:
        action = self.action
        source = self.source
        count = self.count
        cfg = self.cfg

        def work() -> None:
            try:
                direction, names, candidates = complete(cfg, action, source, count)
                payload: tuple = ("ok", direction, names, candidates)
            except Exception as exc:
                payload = ("err", str(exc))
            GLib.idle_add(self._on_result, payload)

        threading.Thread(target=work, daemon=True).start()

    def _on_result(self, payload: tuple) -> bool:
        if self.closed or self.phase == "notice":
            return False
        self._clear_body()
        if payload[0] == "err":
            self.phase = "notice"
            self.body.pack_start(self._wrapped(payload[1] or "请求失败"), False, False, 0)
            self.hint.set_text("Esc 关闭")
        else:
            _status, direction, names, candidates = payload
            self.candidates = candidates
            self.phase = "results"
            if direction:
                self.subtitle.set_text(direction)
                self.subtitle.show()
            for index, text in enumerate(candidates):
                name = names[index] if index < len(names) else str(index + 1)
                self.body.pack_start(self._candidate(index, name, text), False, False, 0)
            keys = "1" if len(candidates) == 1 else "1–" + str(len(candidates))
            extra = " 或 Enter" if len(candidates) == 1 else ""
            if self.surface == "editable":
                self.hint.set_text(f"按 {keys}{extra} 替换原窗口选区，并留在剪贴板。Esc 取消")
            else:
                self.hint.set_text(f"按 {keys}{extra} 或点击，复制到剪贴板。Esc 关闭")
        self._font_on(self.body)
        self.body.show_all()
        self._move_on_screen()
        return False

    def _candidate(self, index: int, name: str, text: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.pack_start(self._wrapped(f"{index + 1}  {name}", hint=True), False, False, 0)
        box.pack_start(self._wrapped(text), False, False, 0)
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_hexpand(True)
        button.add(box)
        button.get_style_context().add_class("candidate")
        button.connect("clicked", lambda _button, pos=index: self._choose(pos))
        return button

    def _lift_if_flush(self, height: int) -> None:
        """Keep one text line between the grown window and the screen bottom.

        New candidates make the window taller downward. The first placement
        still has the gap; this moves the whole window up once the real
        height is known.
        """
        if self.closed or self.user_moved or not self._placed or self._lifting:
            return
        x, y = self.window.get_position()
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = display.get_monitor_at_point(x, y + height)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        edge = 8
        overflow = y + height - (geo.y + geo.height - self._line_height())
        if overflow <= 0:
            return
        new_y = max(geo.y + edge, y - overflow)
        if new_y == y:
            return
        self._lifting = True
        GLib.idle_add(self._move_up, x, new_y)

    def _move_up(self, x: int, y: int) -> bool:
        self._lifting = False
        if not self.closed and not self.user_moved:
            self.window.move(x, y)
        return False

    def _line_height(self) -> int:
        """Vertical size of one character in the popup font."""
        metrics = self.window.get_pango_context().get_metrics(self._font, None)
        height = (metrics.get_ascent() + metrics.get_descent()) // Pango.SCALE
        return height if height > 0 else 16

    def _anchor(self) -> tuple[int, int]:
        if self.user_moved:
            return self.window.get_position()
        px, py = self.pointer
        return px + 16, py + 16

    def _limit_height(self) -> None:
        px, py = self.window.get_position() if self.user_moved else self.pointer
        monitor = Gdk.Display.get_default().get_monitor_at_point(px, py)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        self.scroll.set_max_content_height(max(120, int(geo.height * 0.6)))
        self.scroll.set_min_content_width(self.cfg.width)

    def _move_on_screen(self) -> None:
        self._limit_height()
        natural = self.window.get_preferred_size()[1]
        width, height = natural.width, natural.height
        ax, ay = self._anchor()
        monitor = Gdk.Display.get_default().get_monitor_at_point(ax, ay)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        edge = 8
        bottom = self._line_height()
        max_x = max(geo.x + edge, geo.x + geo.width - width - edge)
        max_y = max(geo.y + edge, geo.y + geo.height - height - bottom)
        self.window.resize(width, height)
        self.window.move(
            min(max(ax, geo.x + edge), max_x),
            min(max(ay, geo.y + edge), max_y),
        )
        self._placed = True


def _number_key(keyval: int) -> int | None:
    mapping = {
        Gdk.KEY_1: 1, Gdk.KEY_2: 2, Gdk.KEY_3: 3, Gdk.KEY_4: 4, Gdk.KEY_5: 5,
        Gdk.KEY_6: 6, Gdk.KEY_7: 7, Gdk.KEY_8: 8, Gdk.KEY_9: 9,
        Gdk.KEY_KP_1: 1, Gdk.KEY_KP_2: 2, Gdk.KEY_KP_3: 3, Gdk.KEY_KP_4: 4,
        Gdk.KEY_KP_5: 5, Gdk.KEY_KP_6: 6, Gdk.KEY_KP_7: 7, Gdk.KEY_KP_8: 8,
        Gdk.KEY_KP_9: 9,
    }
    return mapping.get(keyval)


def notice(cfg: Config, backup: ClipboardBackup, title: str, message: str) -> None:
    Popup(cfg, backup).present_notice(title, message)


def interact(
    cfg: Config,
    backup: ClipboardBackup,
    action: str,
    surface: str,
    source: str,
    count: int,
    window_id: int = 0,
) -> None:
    Popup(cfg, backup).present_request(
        action, surface, source, count, window_id,
    )


def available() -> bool:
    return bool(__import__("os").environ.get("DISPLAY"))
