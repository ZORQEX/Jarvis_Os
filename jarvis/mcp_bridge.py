"""MCP bridge: lets the Claude conversation call JARVIS tools.

Claude Code starts this as an MCP server (stdio, JSON-RPC 2.0, one message per line). Every tool call is forwarded
to the running JARVIS core, which executes it through the Gateway — validation, policy, the user's approval, run,
verification and audit — exactly like any other action. This process has no authority of its own: it only knows the
loopback URL and a per-run secret, read from a 0600 file written by the core.

Usage (by Claude Code only): python -m jarvis.mcp_bridge <bridge.json>
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"


def main() -> None:
    cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())
    tools = cfg["tools"]

    def send(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call(name: str, args: dict) -> tuple[str, bool]:
        req = urllib.request.Request(cfg["url"], data=json.dumps({"name": name, "arguments": args}).encode(),
                                     headers={"Content-Type": "application/json", "X-JARVIS-MCP": cfg["secret"]},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=cfg.get("timeout_s")) as r:     # None = as long as the job takes
                out = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return f"JARVIS отказал: HTTP {e.code}", True
        except (OSError, ValueError) as e:
            return f"JARVIS недоступен: {e.__class__.__name__}", True
        return json.dumps(out.get("result", out), ensure_ascii=False), out.get("result", {}).get("status") not in (None, "ok")

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "jarvis", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            text, is_error = call(str(p.get("name")), p.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}],
                                                         "isError": is_error}})
        elif method == "ping" and mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
