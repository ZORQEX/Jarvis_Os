"""Coding agents on this computer (Claude Code — both accounts — and Codex): live sessions, what each is doing, when
one finished or waits for the person, and (opt-in) their permission requests answered from JARVIS's window.
Ported from morph-rq/jarvis-os (agents.py, agent_hooks.py); the idea is Coucou's (MIT, louis-cfm/coucou).

The agents tell us themselves through their documented hook systems: `bin/jarvis-agent-hook` is registered as a command
hook, reads the hook's JSON on stdin and forwards it over the core's owner-only Unix socket as
{"op": "agent_hook", "agent": "claude", "event": {...}}. Nothing reads transcripts or polls.

Only PermissionRequest waits for an answer, and only when the person turned it on («Отвечать из окна JARVIS»):
  - no window acknowledged the card within ACK_S (none is open)     -> no decision
  - the person didn't answer within `wait_s`                         -> no decision
  - the agent moved on (Stop, a new prompt, the tool ran)            -> withdrawn, no decision
"No decision" makes the hook print nothing, so the agent asks in its terminal as if JARVIS weren't there: JARVIS never
blocks an agent and never answers for the person. Answers are window clicks only (never voice or the phone).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any

AGENTS = {"claude": "Claude Code", "codex": "Codex"}
ACK_S = 2.0               # a window must show the card this fast, or the terminal asks instead
SESSION_TTL_S = 45 * 60   # sessions that went quiet are forgotten
MAX_LINE = 1_000_000
MARKER = "jarvis-agent-hook"

# Event -> hook timeout (s). Status events return at once; PermissionRequest only when the person turned it on.
STATUS_EVENTS = {"SessionStart": 10, "UserPromptSubmit": 10, "PreToolUse": 10, "PostToolUse": 10,
                 "PostToolUseFailure": 10, "Notification": 10, "Stop": 10, "SessionEnd": 5}
CODEX_EVENTS = {"SessionStart": 10, "UserPromptSubmit": 10, "PreToolUse": 10, "PostToolUse": 10, "Stop": 10}


def socket_path() -> Path:
    """$XDG_RUNTIME_DIR/jarvis/hooks.sock (a dev/test core: under its JARVIS_HOME). The relay finds it the same way."""
    if os.environ.get("JARVIS_HOOK_SOCKET"):
        return Path(os.environ["JARVIS_HOOK_SOCKET"])
    if os.environ.get("JARVIS_HOME"):
        return Path(os.environ["JARVIS_HOME"]) / "state" / "hooks.sock"
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(run) / "jarvis" / "hooks.sock"


def _short(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(tool: str, args: Any) -> str:
    """One line a person can read: «Bash · npm test», «Edit · src/app.py»."""
    a = args if isinstance(args, dict) else {}
    detail = ""
    if tool in ("Bash", "shell", "exec_command", "local_shell"):
        cmd = a.get("command")
        detail = " ".join(cmd) if isinstance(cmd, list) else (cmd or "")
    elif tool in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit", "apply_patch"):
        detail = a.get("file_path") or a.get("notebook_path") or a.get("path") or ""
        if detail and os.sep in detail:
            detail = os.path.join(*detail.split(os.sep)[-2:])
    elif tool in ("WebFetch", "WebSearch"):
        detail = a.get("url") or a.get("query") or ""
    elif tool in ("Grep", "Glob"):
        detail = a.get("pattern") or ""
    elif tool in ("Task", "Agent"):
        detail = a.get("description") or ""
    else:
        for k in ("description", "command", "path", "query", "prompt"):
            if isinstance(a.get(k), str):
                detail = a[k]
                break
    return _short(f"{tool} · {detail}" if detail else tool, 160)


def risk_of(tool: str) -> str:
    if tool in ("Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"):
        return "low"
    if tool in ("Bash", "shell", "exec_command", "local_shell"):
        return "high"
    return "moderate"


def _clip(value: Any, depth: int = 0) -> Any:
    """Tool input for the card: small, plain and bounded (shown, never executed by JARVIS)."""
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:1999] + "…"
    if isinstance(value, dict) and depth < 3:
        return {str(k)[:60]: _clip(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list) and depth < 3:
        return [_clip(v, depth + 1) for v in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


class CodingAgents:
    def __init__(self, db, bus, audit) -> None:
        self.db, self.bus, self.audit = db, bus, audit
        self.sessions: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}        # request id -> {session, tool, line, risk, input, ...}
        self._waiters: dict[str, asyncio.Future] = {}
        self._acks: dict[str, asyncio.Event] = {}
        self._by_session: dict[str, str] = {}      # session key -> request id waiting for it
        self._server: asyncio.AbstractServer | None = None
        self.windows = lambda: 0                   # how many JARVIS windows are connected (set by the server)
        self.notify = None                         # (kind, title, body) -> None: Proactive.say

    # ---------------------------------------------------------------- settings
    @property
    def answer_permissions(self) -> bool:
        return bool(self.db.get_kv("coding_answer_permissions", False))

    @property
    def wait_s(self) -> float:
        return float(self.db.get_kv("coding_wait_s", 60) or 60)

    def settings(self, answer: bool | None = None, wait_s: float | None = None) -> dict:
        if answer is not None:
            self.db.set_kv("coding_answer_permissions", bool(answer))
        if wait_s is not None:
            self.db.set_kv("coding_wait_s", max(10.0, min(110.0, float(wait_s))))
        return {"answer_permissions": self.answer_permissions, "wait_s": self.wait_s, "hooks": hooks_status(),
                "socket": str(socket_path()), "relay": relay_path()}

    # ---------------------------------------------------------------- view
    def public(self) -> dict:
        self._expire()
        return {"sessions": sorted(self.sessions.values(), key=lambda s: s["updated"], reverse=True),
                "requests": list(self.requests.values())}

    def _publish(self, changed: str = "") -> None:
        self.bus.publish("coding.agents", {**self.public(), "changed": changed})

    def _expire(self) -> None:
        cutoff = time.time() - SESSION_TTL_S
        for k in [k for k, s in self.sessions.items() if s["updated"] < cutoff and k not in self._by_session]:
            del self.sessions[k]

    def summary(self) -> str:
        """For «что делают агенты?»: one line per live session."""
        rows = self.public()["sessions"]
        if not rows:
            return "Сейчас ни одна сессия Claude Code или Codex не сообщала о работе."
        words = {"idle": "ждёт задачу", "thinking": "думает", "working": "работает", "waiting": "ждёт вас",
                 "permission": "ждёт разрешения", "question": "задал вопрос", "done": "закончил", "error": "ошибка"}
        out = []
        for s in rows[:6]:
            line = f"{s['label']}{' · ' + s['project'] if s['project'] else ''}: {words.get(s['state'], s['state'])}"
            if s["activity"]:
                line += f" — {s['activity']}"
            elif s["state"] == "done" and s["summary"]:
                line += f" — {s['summary'][:120]}"
            out.append(line)
        return "\n".join(out)

    # ---------------------------------------------------------------- events
    def _session(self, agent: str, ev: dict, account: str = "") -> dict:
        sid = str(ev.get("session_id") or ev.get("thread_id") or ev.get("conversation_id") or "default")[:80]
        key = f"{agent}:{sid}"
        s = self.sessions.get(key)
        if s is None:
            cwd = str(ev.get("cwd") or "")
            label = AGENTS.get(agent, agent) + (f" ({account})" if account else "")
            s = {"id": key, "agent": agent, "label": label, "session": sid, "cwd": cwd,
                 "project": os.path.basename(cwd.rstrip("/\\")) or cwd, "state": "idle", "activity": "", "prompt": "",
                 "summary": "", "tools": 0, "started": time.time(), "updated": time.time(),
                 "mode": str(ev.get("permission_mode") or "")}
            self.sessions[key] = s
        return s

    def handle(self, agent: str, ev: dict, account: str = "") -> dict:
        """A non-blocking hook event."""
        agent = agent if agent in AGENTS else "claude"
        name = str(ev.get("hook_event_name") or ev.get("event") or "")
        s = self._session(agent, ev, account)
        s["updated"] = time.time()
        if ev.get("permission_mode"):
            s["mode"] = str(ev["permission_mode"])
        tool = str(ev.get("tool_name") or "")
        before = s["state"]
        if name == "SessionStart":
            s.update(state="idle", activity="")
        elif name == "UserPromptSubmit":
            self._withdraw(s["id"])
            s.update(state="thinking", prompt=_short(ev.get("prompt")), activity="", summary="")
        elif name == "PreToolUse":
            s.update(state="working", activity=describe(tool, ev.get("tool_input")))
            s["tools"] += 1
        elif name in ("PostToolUse", "PostToolUseFailure"):
            self._withdraw(s["id"], tool)        # the tool ran: the person answered in the terminal
            if name == "PostToolUseFailure":
                s["activity"] = "✕ " + describe(tool, ev.get("tool_input"))
            s["state"] = "working"
        elif name == "Notification":
            kind = str(ev.get("notification_type") or "")
            msg = str(ev.get("message", ""))
            if kind in ("idle_prompt", "agent_needs_input", "permission_prompt") or (not kind and msg.endswith("?")):
                s.update(state="waiting", activity=_short(msg, 160))
        elif name == "Stop":
            self._withdraw(s["id"])
            last = str(ev.get("last_assistant_message") or "").strip().split("\n")[0]
            s.update(state="done", activity="", summary=_short(last, 200))
            if before in ("working", "thinking") and s["tools"] >= 3 and self.notify:
                self.notify("coding_done", f"{s['label']} закончил" + (f" · {s['project']}" if s["project"] else ""),
                            s["summary"][:160])
        elif name == "StopFailure":
            self._withdraw(s["id"])
            s.update(state="error", activity=_short(ev.get("error") or ev.get("message") or "остановился с ошибкой", 200))
        elif name == "SessionEnd":
            self._withdraw(s["id"])
            self.sessions.pop(s["id"], None)
            self._publish(s["id"])
            return s
        self._publish(s["id"])
        return s

    async def permission(self, agent: str, ev: dict, account: str = "") -> dict:
        """PermissionRequest: ask the window; {"decision": "allow"|"deny"|None, "message": str}."""
        agent = agent if agent in AGENTS else "claude"
        s = self._session(agent, ev, account)
        s["updated"] = time.time()
        tool = str(ev.get("tool_name") or "tool")
        args = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
        line = describe(tool, args)
        if not self.answer_permissions:
            s.update(state="permission", activity=line)
            self._publish(s["id"])
            return {"decision": None, "message": "answering from JARVIS is off"}
        if tool == "AskUserQuestion":
            s.update(state="question", activity=_short((args.get("questions") or [{}])[0].get("question")
                                                       if isinstance(args.get("questions"), list) and args["questions"]
                                                       and isinstance(args["questions"][0], dict) else args.get("question"), 200))
            self._publish(s["id"])
            return {"decision": None, "message": "questions are answered in the terminal"}
        if s["id"] in self._by_session:
            return {"decision": None, "message": "another request from this session is waiting"}
        if not self.windows():
            s.update(state="permission", activity=line)
            self._publish(s["id"])
            return {"decision": None, "message": "no JARVIS window"}
        rid = "cr_" + uuid.uuid4().hex[:12]
        req = {"id": rid, "session": s["id"], "agent": agent, "label": s["label"], "project": s["project"],
               "cwd": s["cwd"], "tool": tool, "line": line, "risk": risk_of(tool), "input": _clip(args),
               "created": time.time(), "expires": time.time() + self.wait_s}
        loop = asyncio.get_running_loop()
        fut, ack = loop.create_future(), asyncio.Event()
        self.requests[rid], self._waiters[rid], self._acks[rid], self._by_session[s["id"]] = req, fut, ack, rid
        s.update(state="permission", activity=line)
        self.audit.write("coding.permission_asked", agent=agent, tool=tool, project=s["project"])
        self._publish(s["id"])
        try:
            try:
                await asyncio.wait_for(ack.wait(), ACK_S)
            except asyncio.TimeoutError:
                return {"decision": None, "message": "no window showed the card"}
            try:
                allowed, reason = await asyncio.wait_for(asyncio.shield(fut), self.wait_s)
            except asyncio.TimeoutError:
                return {"decision": None, "message": "not answered in time"}
            if allowed is None:
                return {"decision": None, "message": reason}
            self.audit.write("coding.permission_" + ("allowed" if allowed else "denied"), agent=agent, tool=tool)
            return {"decision": "allow" if allowed else "deny",
                    "message": "" if allowed else "Запрещено в окне JARVIS."}
        finally:
            for d in (self.requests, self._waiters, self._acks):
                d.pop(rid, None)
            if self._by_session.get(s["id"]) == rid:
                del self._by_session[s["id"]]
            if s["id"] in self.sessions and s["state"] == "permission":
                s.update(state="working" if fut.done() and fut.result()[0] is not None else "waiting", updated=time.time())
            self._publish(s["id"])

    def ack(self, rid: str) -> bool:
        ev = self._acks.get(rid)
        if ev is not None:
            ev.set()
        return ev is not None

    def decide(self, rid: str, allow: bool) -> dict:
        fut = self._waiters.get(rid)
        if fut is None or fut.done():
            raise ValueError("Запрос уже закрыт: агент спросил в терминале или пошёл дальше")
        self._acks.get(rid, asyncio.Event()).set()
        fut.set_result((bool(allow), ""))
        return {"id": rid, "decision": "allow" if allow else "deny"}

    def _withdraw(self, key: str, tool: str = "") -> None:
        rid = self._by_session.get(key)
        if not rid:
            return
        req = self.requests.get(rid)
        fut = self._waiters.get(rid)
        if req and fut and not fut.done() and (not tool or req["tool"] == tool):
            fut.set_result((None, "the agent moved on"))

    # ---------------------------------------------------------------- the socket
    async def start(self) -> None:
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.exists() or path.is_symlink():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_LINE)
        os.chmod(path, 0o600)

    async def stop(self) -> None:
        for fut in list(self._waiters.values()):
            if not fut.done():
                fut.set_result((None, "JARVIS is stopping"))
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            try:
                socket_path().unlink()
            except OSError:
                pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            msg = json.loads(line or b"{}")
            if not isinstance(msg, dict) or msg.get("op") != "agent_hook" or not isinstance(msg.get("event"), dict):
                return
            agent, ev = str(msg.get("agent") or "claude"), msg["event"]
            account = str(msg.get("account") or "")[:40]
            if ev.get("hook_event_name") == "PermissionRequest":
                out = await self.permission(agent, ev, account)
            else:
                self.handle(agent, ev, account)
                out = {"ok": True}
            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (asyncio.TimeoutError, ValueError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()


# ---------------------------------------------------------------- the agents' hook settings

def relay_path() -> str:
    """The relay the hooks run: ~/.local/bin/jarvis-agent-hook when installed, else the repo's copy."""
    home = Path.home() / ".local" / "bin" / MARKER
    return str(home) if home.exists() else str(Path(__file__).resolve().parents[2] / "bin" / MARKER)


def targets(home: Path | None = None) -> dict[str, Path]:
    """Every hook settings file of a coding agent on this computer: both Claude Code accounts, Codex."""
    home = home or Path.home()
    out = {"claude": home / ".claude" / "settings.json"}
    for d in sorted(home.glob(".claude-*")):
        if d.is_dir() and ((d / ".claude.json").exists() or (d / "settings.json").exists()):
            out[f"claude:{d.name[len('.claude-'):]}"] = d / "settings.json"
    if (home / ".codex").is_dir():
        out["codex"] = home / ".codex" / "hooks.json"
    return out


def _command(agent: str, binary: str, account: str) -> str:
    quoted = "'" + binary.replace("'", "'\\''") + "'"
    cmd = quoted + (" --agent codex" if agent == "codex" else "")
    return cmd + (f" --account {account}" if account else "")


def _strip(hooks: dict) -> dict:
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            inner = [h for h in group.get("hooks", []) if not (isinstance(h, dict) and MARKER in str(h.get("command", "")))]
            if inner:
                kept.append({**group, "hooks": inner})
            elif not group.get("hooks"):
                kept.append(group)
        if kept:
            out[event] = kept
    return out


def merged(settings: dict, agent: str, binary: str, account: str = "", permissions: bool = False,
           wait_s: float = 60) -> dict:
    """settings with exactly one JARVIS entry per event (idempotent). Everything else is kept exactly."""
    events = dict(CODEX_EVENTS if agent == "codex" else STATUS_EVENTS)
    if permissions:
        events["PermissionRequest"] = int(min(120, wait_s + 10))
    hooks = _strip(settings.get("hooks", {}))
    for event, timeout in events.items():
        hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": _command(agent, binary, account),
                                                       "timeout": timeout}]})
    return {**settings, "hooks": hooks}


def removed(settings: dict) -> dict:
    out = dict(settings)
    hooks = _strip(settings.get("hooks", {}))
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def installed_events(settings: dict) -> list[str]:
    return sorted(event for event, groups in (settings.get("hooks") or {}).items()
                  if any(MARKER in str(h.get("command", "")) for g in groups if isinstance(g, dict)
                         for h in g.get("hooks", []) if isinstance(h, dict)))


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} — не JSON-объект")
    return data


def _write(path: Path, data: dict) -> Path | None:
    """Backed up first, written atomically, with the file's own permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = 0o600
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        backup = path.with_name(f"{path.name}.jarvis-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".jarvis-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return backup


def install_hooks(permissions: bool = False, wait_s: float = 60, home: Path | None = None,
                  binary: str | None = None) -> list[dict]:
    """Register the relay in every coding agent's hook settings. An unreadable file is never overwritten."""
    out = []
    for name, path in targets(home).items():
        agent, _, account = name.partition(":")
        try:
            current = _read(path)
        except (OSError, ValueError) as e:
            out.append({"target": name, "path": str(path), "ok": False, "error": str(e)[:200]})
            continue
        new = merged(current, agent, binary or relay_path(), account, permissions and agent == "claude", wait_s)
        if new == current:
            out.append({"target": name, "path": str(path), "ok": True, "changed": False})
            continue
        backup = _write(path, new)
        out.append({"target": name, "path": str(path), "ok": True, "changed": True, "backup": str(backup or "")})
    return out


def uninstall_hooks(home: Path | None = None) -> list[dict]:
    out = []
    for name, path in targets(home).items():
        try:
            current = _read(path)
        except (OSError, ValueError) as e:
            out.append({"target": name, "path": str(path), "ok": False, "error": str(e)[:200]})
            continue
        if not installed_events(current):
            continue
        backup = _write(path, removed(current))
        out.append({"target": name, "path": str(path), "ok": True, "changed": True, "backup": str(backup or "")})
    return out


def hooks_status(home: Path | None = None) -> dict:
    out = {}
    for name, path in targets(home).items():
        try:
            out[name] = {"path": str(path), "events": installed_events(_read(path)), "readable": True}
        except (OSError, ValueError):
            out[name] = {"path": str(path), "events": [], "readable": False}
    return out


# ---------------------------------------------------------------- the conversation's view

from ..tools.base import Risk, Tool, ToolResult, ToolSpec, Verification   # noqa: E402


class CodingStatus(Tool):
    def __init__(self, agents: CodingAgents):
        self.agents = agents
        self.spec = ToolSpec(
            id="coding_agents", title="Агенты-программисты",
            description=("What the coding agents on this computer are doing right now: each Claude Code (both "
                         "accounts) and Codex session — project, state (working / waiting for the person / done), the "
                         "current step, what it finished. Known only from sessions whose hooks report to JARVIS."),
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=5,
            verification="Состояние взято из событий самих агентов")

    async def run(self, args, ctx):
        data = self.agents.public()
        hooks = hooks_status()
        if not any(h["events"] for h in hooks.values()):
            return ToolResult({**data, "hooks": hooks}, "Агенты пока не сообщают JARVIS о работе: подключите их в "
                                                        "«Возможности → На компьютере».")
        return ToolResult({**data, "hooks": hooks}, self.agents.summary())

    async def verify(self, args, result, ctx):
        return Verification("verified", f"Сессий: {len(result.data.get('sessions') or [])}")
