"""A popup that does not take focus, so the original selection can survive."""

from __future__ import annotations

import math
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from ai_translator.config import Config
from ai_translator.llm import complete, system_prompt
from ai_translator.x11util import ClipboardBackup, paste_text, pointer_position

TITLES = {
    "translate": "互译",
    "polish": "润色",
    "translate_readonly": "英译中",
}


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
        self.grabbed = False
        self.phase = "notice"
        self.surface = "readonly"
        self.action = "translate"
        self.source = ""
        self.count = 1
        self.api_key = ""
        self.candidates: list[str] = []
        self.source_window = 0
        self.pointer = pointer_position()
        self.user_moved = False
        self._drag_offset: tuple[float, float] | None = None
        self._pointer_grabbed = False
        self._shaped = (0, 0)

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_name("ai-translator")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
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
        self.window.realize()
        gdk_window = self.window.get_window()
        if gdk_window is not None:
            gdk_window.set_override_redirect(True)
        self._limit_height()
        self.window.show_all()
        if not self.subtitle.get_text():
            self.subtitle.hide()
        self._font_on(self.window)
        self._move_on_screen()
        self._install_drag_cursor()
        Gdk.flush()
        self._grab()

    def _grab(self) -> None:
        seat = Gdk.Display.get_default().get_default_seat()
        gdk_window = self.window.get_window()
        if seat is None or gdk_window is None:
            return
        # The first grab right after map is often rejected while focus settles.
        # Hammering the request keeps it rejected, so wait once and try again.
        # Keyboard only: a pointer grab is unnecessary and fails more often.
        caps = Gdk.SeatCapabilities.KEYBOARD
        for delay in (0, 0.25):
            if delay:
                time.sleep(delay)
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
            status = seat.grab(gdk_window, caps, True, None, None, None)
            if status == Gdk.GrabStatus.SUCCESS:
                self.grabbed = True
                return

    def _ungrab(self) -> None:
        self._end_drag()
        if not self.grabbed:
            return
        seat = Gdk.Display.get_default().get_default_seat()
        if seat is not None:
            seat.ungrab()
        self.grabbed = False

    def _on_destroy(self, _widget) -> None:
        self.closed = True
        self._ungrab()

    def _finish(self, restore: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self._ungrab()
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
        # so the release still arrives after the pointer leaves the bar. The
        # keyboard grab is a different device, so number keys and Esc remain.
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
        if self.phase == "privacy" and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._acknowledge_and_request()
            return True
        if self.phase == "notice" and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._finish(restore=True)
            return True
        if self.phase != "results" or self.surface != "editable":
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
        if self.surface != "editable":
            return
        text = self.candidates[index]
        self.closed = True
        self._ungrab()
        self.window.hide()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            paste_text(text, self.backup, self.cfg.clipboard_restore_ms, self.source_window)
        finally:
            self.window.destroy()
            if Gtk.main_level() > 0:
                Gtk.main_quit()

    def present_notice(self, title: str, message: str) -> None:
        self.phase = "notice"
        self.title.set_text(title)
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
        api_key: str,
        needs_privacy: bool,
        window_id: int,
    ) -> None:
        self.action = action
        self.surface = surface
        self.source = source
        self.count = count
        self.api_key = api_key
        self.source_window = window_id
        self.title.set_text(TITLES.get(action, "ai-translator"))
        if needs_privacy:
            self.phase = "privacy"
            self._clear_body()
            self.body.pack_start(
                self._wrapped("选中的文字会发送到配置的模型接口。按 Enter 继续，Esc 取消。"),
                False, False, 0,
            )
            self.hint.set_text("仅第一次询问")
        else:
            self.phase = "loading"
            self._clear_body()
            self.body.pack_start(self._wrapped("处理中…"), False, False, 0)
            self.hint.set_text("Esc 取消")
        self._show()
        if not needs_privacy:
            self._start_thread()
        Gtk.main()

    def _acknowledge_and_request(self) -> None:
        path = self.cfg.privacy_ack_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
        self.phase = "loading"
        self._clear_body()
        self.body.pack_start(self._wrapped("处理中…"), False, False, 0)
        self.hint.set_text("Esc 取消")
        self._font_on(self.body)
        self.body.show_all()
        self._start_thread()

    def _start_thread(self) -> None:
        action = self.action
        source = self.source
        count = self.count
        api_key = self.api_key
        cfg = self.cfg

        def on_cold() -> None:
            GLib.idle_add(
                self._show_waiting,
                "正在加载模型…",
                "第一次大约要半分钟，同一段时间内再译会快很多",
            )

        def work() -> None:
            try:
                system, names = system_prompt(cfg, action, count)
                direction, candidates = complete(cfg, api_key, system, source, on_cold=on_cold)
                payload: tuple = ("ok", direction, names, candidates[:count])
            except Exception as exc:
                payload = ("err", str(exc))
            GLib.idle_add(self._on_result, payload)

        threading.Thread(target=work, daemon=True).start()

    def _show_waiting(self, body: str, hint: str) -> bool:
        if self.closed or self.phase != "loading":
            return False
        self._clear_body()
        self.body.pack_start(self._wrapped(body), False, False, 0)
        self.hint.set_text(hint)
        self._font_on(self.body)
        self.body.show_all()
        return False

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
            if self.surface == "editable":
                keys = "1" if len(candidates) == 1 else "1–" + str(len(candidates))
                extra = " 或 Enter" if len(candidates) == 1 else ""
                if not self.grabbed:
                    self.hint.set_text("键盘抓取失败，请点击一条候选。结果会留在剪贴板。Esc 取消")
                else:
                    self.hint.set_text(f"按 {keys}{extra} 替换原窗口选区，并留在剪贴板。Esc 取消")
            else:
                self.hint.set_text("Esc 关闭")
        self._font_on(self.body)
        self.body.show_all()
        self._move_on_screen()
        return False

    def _candidate(self, index: int, name: str, text: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.pack_start(self._wrapped(f"{index + 1}  {name}", hint=True), False, False, 0)
        box.pack_start(self._wrapped(text), False, False, 0)
        if self.surface != "editable":
            box.get_style_context().add_class("candidate")
            return box
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_hexpand(True)
        button.add(box)
        button.get_style_context().add_class("candidate")
        button.connect("clicked", lambda _button, pos=index: self._choose(pos))
        return button

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
        max_x = max(geo.x + 8, geo.x + geo.width - width - 8)
        max_y = max(geo.y + 8, geo.y + geo.height - height - 8)
        self.window.resize(width, height)
        self.window.move(
            min(max(ax, geo.x + 8), max_x),
            min(max(ay, geo.y + 8), max_y),
        )


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
    api_key: str,
    window_id: int = 0,
) -> None:
    needs_privacy = not cfg.privacy_ack_path().exists()
    Popup(cfg, backup).present_request(
        action, surface, source, count, api_key, needs_privacy, window_id,
    )


def available() -> bool:
    return bool(__import__("os").environ.get("DISPLAY"))
