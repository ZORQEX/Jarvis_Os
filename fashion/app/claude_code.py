"""Claude through the local Claude Code CLI — the owner's Claude subscription — instead of an API key.

Chosen with ``FASHION_AI_PROVIDER=claude-code`` (JARVIS sets it). The model gets exactly the typed tools of the API
path (``agent.TOOLS``), offered over MCP: Claude Code starts ``app/mcp_bridge.py``, which forwards every tool call
over a private Unix socket (mode 0700 folder) back to the request that started the turn. That request executes it
with ``agent.run_tool`` on its own database session, in its own thread, so the model still cannot save anything
itself: changes stay proposals until «Подтвердить». Claude Code's own tools (shell, files, web) are switched off.
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

BRIDGE = Path(__file__).with_name("mcp_bridge.py")
MAX_CALLS = 16


def cli() -> str:
    return os.environ.get("FASHION_CLAUDE_CLI") or "claude"


def model() -> str:
    return os.environ.get("FASHION_CLAUDE_MODEL") or "sonnet"


def timeout() -> float:
    try:
        return float(os.environ.get("FASHION_CLAUDE_TIMEOUT") or 240)
    except ValueError:
        return 240.0


def available() -> bool:
    return bool(shutil.which(cli()))


def _env() -> dict:
    """No parent Claude session variables and no API secrets: the CLI uses its own login (the subscription)."""
    out = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("CLAUDE") or any(s in upper for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            continue
        out[key] = value
    return out


class TurnError(ValueError):
    """Claude Code did not answer (not installed, not logged in, timed out). ``agent.handle`` then answers by rules."""


def run_turn(system: str, prompt: str, tools: list[dict], execute: Callable[[str, dict], dict],
             limit: float | None = None) -> str:
    """One assistant turn. ``execute(name, args)`` runs every tool call in THIS thread; returns Claude's final text."""
    if not available():
        raise TurnError("Claude Code не установлен.")
    limit = limit or timeout()
    with tempfile.TemporaryDirectory(prefix="fashion-claude-") as tmp:
        os.chmod(tmp, 0o700)
        sock_path = Path(tmp) / "tools.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(4)
        server.setblocking(False)
        bridge_cfg = Path(tmp) / "bridge.json"
        bridge_cfg.write_text(json.dumps({"socket": str(sock_path), "tools": [
            {"name": t["name"], "description": t["description"][:2000], "inputSchema": t["input_schema"]} for t in tools]},
            ensure_ascii=False), "utf-8")
        mcp_cfg = Path(tmp) / "mcp.json"
        mcp_cfg.write_text(json.dumps({"mcpServers": {"fashion": {"command": sys.executable,
                                                                  "args": ["-I", str(BRIDGE), str(bridge_cfg)]}}}), "utf-8")
        argv = [cli(), "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", str(mcp_cfg), "--disable-slash-commands",
                "--permission-mode", "dontAsk", "--tools", "",
                "--allowedTools", ",".join(f"mcp__fashion__{t['name']}" for t in tools),
                "--model", model(), "--system-prompt", system]
        try:
            proc = subprocess.Popen(argv, cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=_env(), start_new_session=True)
        except OSError as exc:
            server.close()
            raise TurnError(f"Claude Code не запустился ({exc.__class__.__name__}).") from None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            return _loop(proc, server, execute, limit)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            server.close()


def _loop(proc: subprocess.Popen, server: socket.socket, execute, limit: float) -> str:
    sel = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(server, selectors.EVENT_READ, "accept")
    buffers: dict = {}
    out_buf, texts, result = b"", [], None
    deadline = time.monotonic() + limit
    calls = 0
    while result is None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TurnError(f"Claude не ответил за {limit:.0f} с.")
        for key, _ in sel.select(timeout=min(left, 1.0)):
            if key.data == "out":
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    sel.unregister(proc.stdout)
                    if result is None:
                        result = {"is_error": True, "result": "Claude Code остановился"}
                    continue
                out_buf += chunk
                while b"\n" in out_buf:
                    line, out_buf = out_buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "assistant":
                        text = "".join(c.get("text", "") for c in (ev.get("message") or {}).get("content") or []
                                       if c.get("type") == "text").strip()
                        if text:
                            texts.append(text)
                    elif ev.get("type") == "result":
                        result = ev
            elif key.data == "accept":
                conn, _ = server.accept()
                conn.setblocking(True)
                buffers[conn] = b""
                sel.register(conn, selectors.EVENT_READ, "conn")
            else:
                conn = key.fileobj
                chunk = conn.recv(1 << 20)
                if not chunk:
                    sel.unregister(conn)
                    conn.close()
                    buffers.pop(conn, None)
                    continue
                buffers[conn] += chunk
                while b"\n" in buffers[conn]:
                    line, buffers[conn] = buffers[conn].split(b"\n", 1)
                    try:
                        req = json.loads(line)
                    except ValueError:
                        continue
                    calls += 1
                    if calls > MAX_CALLS:
                        output = {"error": "Слишком много шагов для одного запроса — остановись и ответь владелице."}
                    else:
                        try:
                            output = execute(str(req.get("name")), req.get("arguments") or {})
                        except Exception as exc:          # a tool failure is the model's information, not a crash
                            output = {"error": f"{exc.__class__.__name__}: {str(exc)[:300]}"}
                    conn.sendall((json.dumps({"id": req.get("id"), "result": output}, ensure_ascii=False,
                                             default=str) + "\n").encode("utf-8"))
    for key in list(sel.get_map().values()):
        if key.data == "conn":
            key.fileobj.close()
    sel.close()
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        message = str(result.get("result") or result.get("subtype") or "ошибка")
        if "login" in message.casefold() or "auth" in message.casefold():
            raise TurnError("Claude Code не вошёл в аккаунт — войдите командой claude.")
        raise TurnError(f"Claude: {message[:200]}")
    return (str(result.get("result") or "").strip() or (texts[-1] if texts else "")).strip()
