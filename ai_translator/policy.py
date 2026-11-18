"""Decide what a hotkey should do for this window and this selection.

Terminals are always read-only. Ctrl+C in a terminal is SIGINT, and a paste
would be typed into the shell rather than replace a selection.
"""

from __future__ import annotations

TERMINAL_WM_CLASSES = frozenset({
    "gnome-terminal",
    "gnome-terminal-server",
    "kgx",
    "org.gnome.console",
    "kitty",
    "alacritty",
    "xterm",
    "konsole",
    "tilix",
    "terminator",
    "wezterm",
    "org.wezfurlong.wezterm",
    "xfce4-terminal",
    "mate-terminal",
    "ghostty",
    "com.mitchellh.ghostty",
    "urxvt",
    "rxvt",
    "st",
    "io.elementary.terminal",
    "terminology",
    "hyper",
    "tabby",
    "contour",
    "foot",
    "footclient",
})

SILENT = "silent"
EMPTY = "empty"
TOO_LONG = "too_long"
TRANSLATE = "translate"
POLISH = "polish"
TRANSLATE_READONLY = "translate_readonly"


def is_han(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def is_latin(char: str) -> bool:
    return ("A" <= char <= "Z") or ("a" <= char <= "z")


def mainly_chinese(text: str, ratio: float) -> bool:
    han = sum(1 for char in text if is_han(char))
    latin = sum(1 for char in text if is_latin(char))
    total = han + latin
    if total == 0:
        return False
    return (han / total) >= ratio


def has_latin(text: str) -> bool:
    return any(is_latin(char) for char in text)


def window_names(instance: str, klass: str) -> set[str]:
    return {name.lower() for name in (instance, klass) if name}


def is_terminal(instance: str, klass: str) -> bool:
    return bool(window_names(instance, klass) & TERMINAL_WM_CLASSES)


def classify_surface(
    instance: str,
    klass: str,
    editable_classes: list[str],
    readonly_classes: list[str],
) -> str:
    names = window_names(instance, klass)
    if not names or names & TERMINAL_WM_CLASSES:
        return "readonly"
    editable = {item.lower() for item in editable_classes}
    if names & editable:
        return "editable"
    readonly = {item.lower() for item in readonly_classes}
    if names & readonly:
        return "readonly"
    # Unknown windows stay read-only. Browsers are editable only when their
    # WM_CLASS is listed; a page body and a text field still share that class.
    return "readonly"


def decide(mode: str, surface: str, text: str | None, max_chars: int, chinese_ratio: float) -> str:
    if mode == "polish" and surface != "editable":
        return SILENT
    if text is None or not text.strip():
        return EMPTY if surface == "editable" else SILENT
    if surface != "editable":
        if mainly_chinese(text, chinese_ratio) or not has_latin(text):
            return SILENT
        if len(text) > max_chars:
            return TOO_LONG
        return TRANSLATE_READONLY
    if len(text) > max_chars:
        return TOO_LONG
    if mode == "polish":
        return POLISH
    return TRANSLATE


def candidate_count(text: str, short_max: int, short_count: int, long_count: int) -> int:
    count = short_count if len(text) <= short_max else long_count
    if count < 1:
        return 1
    if count > 9:
        return 9
    return count


def style_slice(names: list[str], hints: list[str], count: int) -> tuple[list[str], list[str]]:
    """Pick the styles that will actually be requested.

    A single candidate uses the "自然" style when that name exists, because
    the long-text path wants one natural wording rather than a literal one.
    """
    if count == 1:
        index = 0
        for pos, name in enumerate(names):
            if name == "自然":
                index = pos
                break
        else:
            if len(names) >= 2:
                index = 1
        name = names[index] if names else "自然"
        hint = hints[index] if index < len(hints) else ""
        return [name], [hint]
    picked_names = []
    picked_hints = []
    for index in range(count):
        picked_names.append(names[index] if index < len(names) else str(index + 1))
        picked_hints.append(hints[index] if index < len(hints) else "")
    return picked_names, picked_hints
