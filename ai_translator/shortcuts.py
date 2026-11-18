"""Register the two hotkeys as GNOME custom shortcuts without dropping existing ones."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ai_translator.config import Config

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


class ShortcutError(RuntimeError):
    pass


def gvariant_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def gvariant_strv(items: list[str]) -> str:
    return "[" + ", ".join(gvariant_string(item) for item in items) + "]"


def parse_strv(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    found: list[str] = []
    for match in re.finditer(r"'([^']*)'|\"((?:\\.|[^\"])*)\"", text):
        if match.group(1) is not None:
            found.append(match.group(1))
        else:
            found.append(bytes(match.group(2), "utf-8").decode("unicode_escape"))
    return found


def merge_paths(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    for path in additions:
        if path not in merged:
            merged.append(path)
    return merged


def _gsettings_get(key: str) -> str:
    try:
        result = subprocess.run(
            ["gsettings", "get", SCHEMA, key],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ShortcutError("找不到 gsettings，无法注册 GNOME 快捷键") from exc
    if result.returncode != 0:
        raise ShortcutError(result.stderr.strip() or "读取 GNOME 快捷键失败")
    return result.stdout


def _gsettings_set(schema: str, key: str, value: str) -> None:
    result = subprocess.run(
        ["gsettings", "set", schema, key, value],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ShortcutError(detail or f"写入 {key} 失败")


def install_shortcuts(cfg: Config, launcher: Path) -> list[tuple[str, str, str]]:
    launcher = launcher.resolve()
    entries = [
        ("ai-translator-translate", "AI Translator Translate", f"{launcher} translate", cfg.translate_hotkey),
        ("ai-translator-polish", "AI Translator Polish", f"{launcher} polish", cfg.polish_hotkey),
    ]
    existing = parse_strv(_gsettings_get("custom-keybindings"))
    obsolete = [f"{PREFIX}{slug}/" for slug in ("ai-trans-translate", "ai-trans-polish")]
    existing = [path for path in existing if path not in obsolete]
    paths = []
    written = []
    for slug, name, command, binding in entries:
        path = f"{PREFIX}{slug}/"
        paths.append(path)
        item_schema = f"{ITEM}:{path}"
        _gsettings_set(item_schema, "name", gvariant_string(name))
        _gsettings_set(item_schema, "command", gvariant_string(command))
        _gsettings_set(item_schema, "binding", gvariant_string(binding))
        written.append((name, binding, command))
    _gsettings_set(SCHEMA, "custom-keybindings", gvariant_strv(merge_paths(existing, paths)))
    return written
