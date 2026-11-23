"""Choose the action for a selection, and turn the model reply into candidates."""

from __future__ import annotations

import json
import re

from ai_translator.config import Config
from ai_translator.deepseek import DeepSeekError, complete as deepseek_complete

EMPTY = "empty"
TOO_LONG = "too_long"
TRANSLATE = "translate"
POLISH = "polish"


class ModelError(RuntimeError):
    pass


def decide(mode: str, _surface: str, text: str | None, max_chars: int) -> str:
    """What the hotkey should do. The caller pastes only into an editable window."""
    if text is None or not text.strip():
        return EMPTY
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


def system_prompt(cfg: Config, action: str, count: int) -> tuple[str, list[str]]:
    names, hints = style_slice(cfg.styles, cfg.style_hints, count)
    # Translate prompts define each candidate. The shared hints are for polish.
    if action == "translate":
        lines = "\n".join(f"{index}. {name}" for index, name in enumerate(names, start=1))
    else:
        lines = "\n".join(
            f"{index}. {name}: {hint}"
            for index, (name, hint) in enumerate(zip(names, hints), start=1)
        )
    template = cfg.polish_system if action == "polish" else cfg.translate_system
    try:
        rendered = template.format(count=count, styles=lines)
    except (KeyError, IndexError, ValueError) as exc:
        raise ModelError("提示词模板只支持 {count} 和 {styles}") from exc
    return rendered, names


def parse_model_json(content: str) -> tuple[str, list[str]]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ModelError("模型没有返回 JSON") from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ModelError("模型返回的 JSON 无法解析") from exc
    if not isinstance(data, dict):
        raise ModelError("模型返回的 JSON 不是对象")
    raw_items = data.get("candidates")
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raise ModelError("模型返回里没有 candidates")
    candidates: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict) and "text" in item:
            value = str(item["text"]).strip()
        else:
            continue
        if value:
            candidates.append(value)
    if not candidates:
        raise ModelError("模型没有给出候选")
    direction = data.get("direction")
    if not isinstance(direction, str):
        direction = ""
    return direction.strip(), candidates


def complete(cfg: Config, action: str, source: str, count: int) -> tuple[str, list[str], list[str]]:
    system, names = system_prompt(cfg, action, count)
    try:
        content = deepseek_complete(cfg, system, source)
    except DeepSeekError as exc:
        raise ModelError(str(exc)) from exc
    direction, candidates = parse_model_json(content)
    return direction, names, candidates[:count]
