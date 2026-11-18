"""Keep one Cursor agent alive so later translations skip model startup.

A fresh agent spends most of the first reply loading the model. Reusing it
brings the next reply down to a few seconds. The process exits after it has
been idle, so it is not a login-time daemon.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from ai_translator.config import load_config

_FRAME_LIMIT = 1_000_000


class BackendError(RuntimeError):
    pass


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "ai-translator.sock"


def send_frame(conn: socket.socket, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn.sendall(len(data).to_bytes(4, "big") + data)


def recv_frame(conn: socket.socket) -> dict:
    header = _recvall(conn, 4)
    if not header:
        raise BackendError("翻译服务断开了")
    size = int.from_bytes(header, "big")
    if size > _FRAME_LIMIT:
        raise BackendError("请求过大")
    body = _recvall(conn, size)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise BackendError("翻译服务返回了无效数据")
    return data


def _recvall(conn: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        part = conn.recv(remaining)
        if not part:
            break
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _can_connect(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(0.2)
        conn.connect(str(path))
        conn.close()
        return True
    except OSError:
        return False


def _spawn_server() -> None:
    script = Path(__file__).resolve().parent.parent / "ai-translator"
    subprocess.Popen(
        [sys.executable, str(script), "serve"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ask(message: str, timeout: float, on_cold=None) -> str:
    path = socket_path()
    if not _can_connect(path):
        if on_cold is not None:
            on_cold()
        stale = path.exists()
        if stale:
            try:
                path.unlink()
            except OSError:
                pass
        _spawn_server()
        deadline = time.monotonic() + min(10.0, timeout)
        while time.monotonic() < deadline:
            if _can_connect(path):
                break
            time.sleep(0.05)
        else:
            raise BackendError("翻译服务没有启动")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        send_frame(conn, {"message": message})
        payload = recv_frame(conn)
    except socket.timeout as exc:
        raise BackendError("模型接口超时") from exc
    finally:
        conn.close()
    if not payload.get("ok"):
        raise BackendError(str(payload.get("error") or "翻译失败"))
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BackendError("模型返回了空内容")
    return text


class _Session:
    def __init__(self) -> None:
        self.client = None
        self.agent = None
        self.signature: tuple | None = None

    def generate(self, message: str) -> str:
        from cursor_sdk import (
            AgentOptions,
            CursorAgentError,
            CursorClient,
            LocalAgentOptions,
            ModelParameterValue,
            ModelSelection,
        )

        cfg = load_config()
        signature = (cfg.path.stat().st_mtime, cfg.model, cfg.fast, cfg.api_key)
        if self.agent is None or signature != self.signature:
            self.close()
            cwd = str(cfg.path.parent)
            self.client = CursorClient.launch_bridge(workspace=cwd)
            params = [ModelParameterValue(id="fast", value="true" if cfg.fast else "false")]
            self.agent = self.client.agents.create(AgentOptions(
                api_key=cfg.api_key,
                model=ModelSelection(id=cfg.model, params=params),
                tools=[],
                local=LocalAgentOptions(cwd=cwd),
            ))
            self.signature = signature
        try:
            result = self.agent.send(message).wait()
        except CursorAgentError as exc:
            self.close()
            raise BackendError(f"Cursor API 调用失败：{exc}") from exc
        if getattr(result, "status", "") != "finished":
            detail = getattr(result, "result", "") or getattr(result, "status", "")
            raise BackendError(f"Cursor API 没有完成：{detail}")
        return str(result.result or "")

    def close(self) -> None:
        agent, client = self.agent, self.client
        self.agent = None
        self.client = None
        self.signature = None
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _handle(conn: socket.socket, session: _Session) -> None:
    try:
        request = recv_frame(conn)
        message = request.get("message")
        if not isinstance(message, str) or not message:
            raise BackendError("请求里没有文本")
        text = session.generate(message)
        send_frame(conn, {"ok": True, "text": text})
    except Exception as exc:
        try:
            send_frame(conn, {"ok": False, "error": str(exc)})
        except Exception:
            pass


def serve() -> int:
    path = socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(4)
    server.settimeout(1.0)
    session = _Session()
    last_used = time.monotonic()
    try:
        while True:
            idle = max(1, load_config().agent_idle_sec)
            if time.monotonic() - last_used > idle:
                break
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            conn.settimeout(30)
            try:
                _handle(conn, session)
            finally:
                conn.close()
            last_used = time.monotonic()
    finally:
        session.close()
        server.close()
        try:
            path.unlink()
        except OSError:
            pass
    return 0
