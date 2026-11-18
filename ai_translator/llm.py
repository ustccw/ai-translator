"""Ask the configured model for candidates."""

from __future__ import annotations

import json
import re

from ai_translator.backend import BackendError, ask
from ai_translator.config import Config
from ai_translator.deepseek import DeepSeekError, complete as deepseek_complete
from ai_translator.policy import style_slice


class ModelError(RuntimeError):
    pass


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
    if action == "polish":
        template = cfg.polish_system
    elif action == "translate_readonly":
        template = cfg.translate_readonly_system
    else:
        template = cfg.translate_system
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


def complete(cfg: Config, api_key: str, system: str, source: str, on_cold=None) -> tuple[str, list[str]]:
    del api_key  # Keys are read from the files named in the config.
    try:
        if cfg.provider == "deepseek":
            content = deepseek_complete(cfg, system, source)
        else:
            message = system + "\n\nSource text:\n" + source
            content = ask(message, cfg.timeout_sec, on_cold=on_cold)
    except (BackendError, DeepSeekError) as exc:
        raise ModelError(str(exc)) from exc
    return parse_model_json(content)
