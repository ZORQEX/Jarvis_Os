"""MCP server for Claude Code (stdio, JSON-RPC 2.0, one message per line): offers the assistant's typed tools and
forwards each call over the turn's private Unix socket to the request that started the turn (app/claude_code.py).
It has no database or authority of its own. Usage (by Claude Code only): python -I mcp_bridge.py <bridge.json>"""
import json
import socket
import sys

PROTOCOL = "2025-06-18"


def main() -> None:
    cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())
    conn, buf, seq = None, b"", 0

    def send(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call(name: str, args: dict) -> dict:
        nonlocal conn, buf, seq
        if conn is None:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(cfg["socket"])
        seq += 1
        conn.sendall((json.dumps({"id": seq, "name": name, "arguments": args}, ensure_ascii=False) + "\n").encode())
        while b"\n" not in buf:
            chunk = conn.recv(1 << 20)
            if not chunk:
                raise OSError("turn closed")
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        return json.loads(line).get("result") or {}

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "fashion", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": cfg["tools"]}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                out = call(str(p.get("name")), p.get("arguments") or {})
                text, is_error = json.dumps(out, ensure_ascii=False, default=str)[:60000], "error" in out
            except (OSError, ValueError) as exc:
                text, is_error = f"Приложение недоступно: {exc.__class__.__name__}", True
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
        elif method == "ping" and mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()
