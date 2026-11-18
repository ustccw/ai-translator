"""One-shot entry: a GNOME shortcut starts this, the window closes, the process exits."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

from ai_translator.config import Config, TomlError, load_config
from ai_translator.policy import (
    EMPTY,
    SILENT,
    TOO_LONG,
    candidate_count,
    classify_surface,
    decide,
)
from ai_translator.shortcuts import ShortcutError, install_shortcuts
from ai_translator.x11util import X11Error, active_wm_class, capture_selection, release_modifiers
from ai_translator import ui


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


def _fail(cfg: Config | None, message: str) -> int:
    print(f"ai-translator: {message}", file=sys.stderr)
    if cfg is not None and ui.available():
        from ai_translator.x11util import ClipboardBackup

        try:
            ui.notice(cfg, ClipboardBackup(), "ai-translator", message)
        except Exception as exc:
            print(f"ai-translator: {exc}", file=sys.stderr)
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
        instance, klass = active_wm_class()
        surface = classify_surface(
            instance, klass, cfg.editable_wm_class, cfg.readonly_wm_class,
        )
        if mode == "polish" and surface != "editable":
            return 0
        text, backup = capture_selection(surface, cfg.copy_wait_ms)
        action = decide(mode, surface, text, cfg.max_chars, cfg.chinese_char_ratio)
        if action == SILENT:
            return 0
        if action == EMPTY:
            ui.notice(cfg, backup, "没有选中文字", "先划选一段文字，再按快捷键。")
            return 0
        if action == TOO_LONG:
            ui.notice(cfg, backup, "选区过长", f"超过 {cfg.max_chars} 字，已取消。")
            return 0
        if cfg.provider == "deepseek":
            api_key, key_path, key_name = cfg.deepseek_api_key, cfg.deepseek_api_key_path, "DeepSeek"
        else:
            api_key, key_path, key_name = cfg.api_key, cfg.api_key_path, "Cursor"
        if not api_key:
            ui.notice(
                cfg,
                backup,
                "缺少 API key",
                f"把 {key_name} 的 API key 写进\n{key_path}\n单独一行，不要加引号。",
            )
            return 0
        assert text is not None
        count = candidate_count(
            text,
            cfg.short_text_max_chars,
            cfg.short_candidate_count,
            cfg.long_candidate_count,
        )
        ui.interact(cfg, backup, action, surface, text, count, api_key)
        return 0
    except (X11Error, OSError, RuntimeError) as exc:
        return _fail(cfg, str(exc))
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
    try:
        written = install_shortcuts(cfg, launcher)
    except ShortcutError as exc:
        print(f"ai-translator: {exc}", file=sys.stderr)
        return 1
    print("已注册快捷键：")
    for _name, binding, command in written:
        print(f"  {binding}")
        print(f"    {command}")
    print(f"修改 {cfg.path} 里的快捷键后，再执行一次 install-shortcuts。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-translator", description="划词后互译或润色，并可在可编辑窗口里写回。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("translate", help="中文和英文对翻")
    sub.add_parser("polish", help="只润色同一种语言，不翻译")
    sub.add_parser("install-shortcuts", help="按配置文件把两个快捷键写进 GNOME")
    sub.add_parser("serve", help="在后台保持一个可复用的翻译 Agent")
    args = parser.parse_args(argv)
    if args.cmd == "install-shortcuts":
        return _install()
    if args.cmd == "serve":
        from ai_translator.backend import serve
        return serve()
    return _run(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
