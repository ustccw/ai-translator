"""One-shot entry: a GNOME shortcut starts this, the window closes, the process exits."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

from ai_translator.config import TomlError, load_config
from ai_translator.prompt import EMPTY, TOO_LONG, candidate_count, decide
from ai_translator.shortcuts import (
    ShortcutError,
    confirm_replace,
    display_hotkey,
    find_conflicts,
    install_shortcuts,
    repeated_hotkey,
)
from ai_translator.windows import classify_surface
from ai_translator.x11util import X11Error, active_window, capture_selection, release_modifiers
from ai_translator import ui


def _runtime_error() -> str | None:
    """Return a message when this process is outside the supported environment."""
    if sys.version_info < (3, 10) or sys.version_info[:2] > (3, 15):
        return "需要 Python 3.10 到 3.15。"
    if os.environ.get("XDG_SESSION_TYPE") != "x11":
        return "需要 X11 桌面。Ubuntu 22.04 和 24.04 请在登录界面选「Ubuntu on Xorg」。"
    return None


def _lock():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return os.open(os.devnull, os.O_RDONLY)
    path = Path(runtime) / "ai-translator.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _fail(message: str) -> int:
    print(f"ai-translator: {message}", file=sys.stderr)
    return 1


def _run(mode: str) -> int:
    held = _lock()
    if held is None:
        return 0
    try:
        cfg = load_config()
    except TomlError as exc:
        print(f"ai-translator: {exc}", file=sys.stderr)
        return 1
    try:
        release_modifiers()
        window_id, instance, klass = active_window()
        surface = classify_surface(
            instance, klass, cfg.editable_wm_class, cfg.readonly_wm_class,
        )
        text, backup = capture_selection(surface, cfg.copy_wait_ms)
        action = decide(mode, surface, text, cfg.max_chars)
        if action == EMPTY:
            ui.notice(cfg, backup, "没有选中文字", "先划选一段文字，再按快捷键。")
            return 0
        if action == TOO_LONG:
            ui.notice(cfg, backup, "选区过长", f"超过 {cfg.max_chars} 字，已取消。")
            return 0
        if not cfg.api_key:
            ui.notice(
                cfg,
                backup,
                "缺少 API key",
                f"把 DeepSeek 的 API key 写进\n{cfg.api_key_path}\n单独一行，不要加引号。",
            )
            return 0
        assert text is not None
        count = candidate_count(
            text,
            cfg.short_text_max_chars,
            cfg.short_candidate_count,
            cfg.long_candidate_count,
        )
        ui.interact(cfg, backup, action, surface, text, count, window_id)
        return 0
    except (X11Error, OSError, RuntimeError) as exc:
        return _fail(str(exc))
    finally:
        os.close(held)


def _install() -> int:
    try:
        cfg = load_config()
    except TomlError as exc:
        print(f"ai-translator: {exc}", file=sys.stderr)
        return 1
    launcher = Path(sys.argv[0]).resolve()
    if launcher.name == "__main__.py":
        launcher = Path(__file__).resolve().parents[1] / "ai-translator"
    hotkeys = [cfg.translate_hotkey, cfg.polish_hotkey]
    duplicate = repeated_hotkey(hotkeys)
    if duplicate is not None:
        print(f"ai-translator: {display_hotkey(duplicate)} 不能同时用于互译和润色。")
        print(f"请编辑 {cfg.path} 里的 [hotkeys]，换成不同的按键，再执行一次 install-shortcuts。")
        return 1
    try:
        conflicts = find_conflicts(hotkeys)
    except ShortcutError as exc:
        print(f"ai-translator: {exc}", file=sys.stderr)
        return 1
    if conflicts and not confirm_replace(conflicts, cfg.path):
        print("未修改快捷键。")
        print(f"请编辑 {cfg.path} 里的 [hotkeys]，换成别的按键，再执行一次 install-shortcuts。")
        return 0
    try:
        written, released = install_shortcuts(cfg, launcher, conflicts)
    except ShortcutError as exc:
        print(f"ai-translator: {exc}", file=sys.stderr)
        return 1
    if released:
        print("已从系统快捷键中去掉同一个按键：")
        for schema, key, value in released:
            shown = value if value else "（空）"
            print(f"  {schema} {key} = {shown}")
    print("已注册快捷键：")
    for _name, binding, command in written:
        print(f"  {binding}")
        print(f"    {command}")
    print("这两个按键现在只用于 ai-translator。")
    print(f"修改 {cfg.path} 里的快捷键后，再执行一次 install-shortcuts。")
    return 0


def main(argv: list[str] | None = None) -> int:
    problem = _runtime_error()
    if problem:
        print(f"ai-translator: {problem}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog="ai-translator", description="划词后互译或润色，并可在可编辑窗口里写回。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("translate", help="中文和英文对翻")
    sub.add_parser("polish", help="只润色同一种语言，不翻译")
    sub.add_parser("install-shortcuts", help="按配置文件把两个快捷键写进 GNOME")
    args = parser.parse_args(argv)
    if args.cmd == "install-shortcuts":
        return _install()
    return _run(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
