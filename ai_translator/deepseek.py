"""DeepSeek chat completions. This path does not start a local agent."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ai_translator.config import Config


class DeepSeekError(RuntimeError):
    pass


def request_body(model: str, thinking: bool, system: str, source: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Source text:\n" + source},
        ],
        "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "response_format": {"type": "json_object"},
    }


def complete(cfg: Config, system: str, source: str) -> str:
    if not cfg.deepseek_api_key:
        raise DeepSeekError(f"把 DeepSeek 的 API key 写进 {cfg.deepseek_api_key_path}")
    body = request_body(cfg.deepseek_model, cfg.deepseek_thinking, system, source)
    request = urllib.request.Request(
        cfg.deepseek_base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg.deepseek_api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip().replace("\n", " ")
        if len(detail) > 240:
            detail = detail[:240]
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc.reason}") from exc
    except (json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekError("DeepSeek 没有返回文本") from exc
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError("DeepSeek 返回了空内容")
    return content
