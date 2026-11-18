"""A popup that does not take focus, so the original selection can survive."""

from __future__ import annotations

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
        self.pointer = pointer_position()

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
        self.window.add_events(Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.window.connect("key-press-event", self._on_key)
        self.window.connect("button-press-event", self._on_button)
        self.window.connect("destroy", self._on_destroy)

        self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.outer.set_border_width(14)
        self.outer.set_size_request(cfg.width, -1)
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("title")
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.get_style_context().add_class("hint")
        self.subtitle.set_no_show_all(True)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hint = Gtk.Label(xalign=0)
        self.hint.get_style_context().add_class("hint")
        self.hint.set_line_wrap(True)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_width(True)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.add(self.body)
        self.outer.pack_start(self.title, False, False, 0)
        self.outer.pack_start(self.subtitle, False, False, 0)
        self.outer.pack_start(self.scroll, True, True, 0)
        self.outer.pack_start(self.hint, False, False, 0)
        self.window.add(self.outer)
        self._apply_style()

    def _apply_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            #ai-translator {
                background-color: #1e1e1e;
                border: 1px solid #5a5a5a;
            }
            #ai-translator label { color: #f3f3f3; }
            #ai-translator .title { font-weight: bold; }
            #ai-translator .hint { color: #a8a8a8; }
            #ai-translator button {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 6px 4px;
            }
            #ai-translator button:hover { background-color: #333333; }
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
            paste_text(text, self.backup, self.cfg.clipboard_restore_ms)
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
    ) -> None:
        self.action = action
        self.surface = surface
        self.source = source
        self.count = count
        self.api_key = api_key
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
                    self.hint.set_text(f"键盘抓取失败，请点击一条候选。Esc 取消")
                else:
                    self.hint.set_text(f"按 {keys}{extra} 替换选区，Esc 取消")
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
            return box
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_hexpand(True)
        button.add(box)
        button.connect("clicked", lambda _button, pos=index: self._choose(pos))
        return button

    def _limit_height(self) -> None:
        px, py = self.pointer
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
        px, py = self.pointer
        monitor = Gdk.Display.get_default().get_monitor_at_point(px, py)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        max_x = max(geo.x + 8, geo.x + geo.width - width - 8)
        max_y = max(geo.y + 8, geo.y + geo.height - height - 8)
        self.window.resize(width, height)
        self.window.move(
            min(max(px + 16, geo.x + 8), max_x),
            min(max(py + 16, geo.y + 8), max_y),
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
) -> None:
    needs_privacy = not cfg.privacy_ack_path().exists()
    Popup(cfg, backup).present_request(action, surface, source, count, api_key, needs_privacy)


def available() -> bool:
    return bool(__import__("os").environ.get("DISPLAY"))
