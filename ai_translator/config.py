"""Load the config.toml that ships next to this package."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ai_translator.tomlutil import TomlError, loads


@dataclass
class Config:
    api_key: str
    api_key_path: Path
    base_url: str
    model: str
    thinking: bool
    timeout_sec: float
    short_text_max_chars: int
    max_chars: int
    short_candidate_count: int
    long_candidate_count: int
    font: str
    width: int
    clipboard_restore_ms: int
    copy_wait_ms: int
    translate_hotkey: str
    polish_hotkey: str
    editable_wm_class: list[str]
    readonly_wm_class: list[str]
    translate_system: str
    polish_system: str
    styles: list[str]
    style_hints: list[str]
    path: Path


def bundled_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.toml"


def _section(data: dict, name: str) -> dict:
    section = data.get(name)
    if not isinstance(section, dict):
        raise TomlError(f"配置缺少 [{name}]")
    return section


def _need(section: dict, key: str, kind: type | tuple[type, ...]):
    if key not in section:
        raise TomlError(f"配置缺少 {key}")
    value = section[key]
    if not isinstance(value, kind):
        raise TomlError(f"配置项 {key} 的类型应为 {kind}")
    return value


def _string_list(section: dict, key: str) -> list[str]:
    value = _need(section, key, list)
    if not all(isinstance(item, str) for item in value):
        raise TomlError(f"配置项 {key} 必须是字符串数组")
    return list(value)


def resolve_api_key_file(raw: str, config_path: Path) -> Path:
    expanded = Path(os.path.expanduser(raw.strip()))
    if not expanded.is_absolute():
        expanded = config_path.parent / expanded
    return expanded


def read_api_key(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise TomlError(f"无法读取 API key 文件 {path}: {exc}") from exc
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        name, sep, value = item.partition("=")
        if sep and name.strip() in {"api_key", "key"}:
            item = value.strip().strip('"').strip("'")
        return item.strip().strip('"').strip("'")
    return ""


def config_from_mapping(merged: dict[str, dict], path: Path) -> Config:
    api = _section(merged, "api")
    deepseek = _section(merged, "deepseek")
    limits = _section(merged, "limits")
    ui = _section(merged, "ui")
    hotkeys = _section(merged, "hotkeys")
    windows = _section(merged, "windows")
    prompts = _section(merged, "prompts")
    key_path = resolve_api_key_file(str(_need(deepseek, "api_key_file", str)), path)
    return Config(
        api_key=read_api_key(key_path),
        api_key_path=key_path,
        base_url=str(_need(deepseek, "base_url", str)).rstrip("/"),
        model=_need(deepseek, "model", str),
        thinking=bool(_need(deepseek, "thinking", bool)),
        timeout_sec=float(_need(api, "timeout_sec", (int, float))),
        short_text_max_chars=int(_need(limits, "short_text_max_chars", int)),
        max_chars=int(_need(limits, "max_chars", int)),
        short_candidate_count=int(_need(limits, "short_candidate_count", int)),
        long_candidate_count=int(_need(limits, "long_candidate_count", int)),
        font=_need(ui, "font", str),
        width=int(_need(ui, "width", int)),
        clipboard_restore_ms=int(_need(ui, "clipboard_restore_ms", int)),
        copy_wait_ms=int(_need(ui, "copy_wait_ms", int)),
        translate_hotkey=_need(hotkeys, "translate", str),
        polish_hotkey=_need(hotkeys, "polish", str),
        editable_wm_class=_string_list(windows, "editable_wm_class"),
        readonly_wm_class=_string_list(windows, "readonly_wm_class"),
        translate_system=_need(prompts, "translate_system", str),
        polish_system=_need(prompts, "polish_system", str),
        styles=_string_list(prompts, "styles"),
        style_hints=_string_list(prompts, "style_hints"),
        path=path,
    )


def load_config(path: Path | None = None) -> Config:
    path = path or bundled_config_path()
    try:
        parsed = loads(path.read_text(encoding="utf-8"))
    except TomlError:
        raise
    except OSError as exc:
        raise TomlError(f"无法读取配置 {path}: {exc}") from exc
    return config_from_mapping(parsed, path)
