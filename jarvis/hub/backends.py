"""OpenClaw and Hermes as supervised local services behind JARVIS (ported from morph-rq/jarvis-os, ADR-008).

Layout (user level only; scripts/install-agents.sh):

    <data>/agents/node/<ver>/        Node 24 for OpenClaw (sha256-checked tarball)
    <data>/agents/openclaw/<ver>/    `npm ci --ignore-scripts` from config/agents/openclaw
    <data>/agents/hermes/<ver>/      a venv from config/agents/hermes.lock (hashes)
    <data>/agents/<name>/current -> <ver>
    <data>/skills-shared/            skills the person approved, read by both agents

Their own state stays where they keep it (~/.openclaw, ~/.hermes; a dev/test JARVIS_HOME gets its own folders).
JARVIS changes only the settings it needs (loopback, token auth, Hermes's toolsets and model, the shared skills folder,
the fast-search MCP server) with their own config tools, and writes each change to agents/migrations.log. Keys (gateway
token, Hermes API key, the key of JARVIS's model endpoint) are never written into their config files: they are passed
in the environment of the child process.

Both agents think with Claude through the person's own Claude Code sign-in (ADR-005): OpenClaw runs the `claude` program
itself (model `claude-cli/…`), Hermes calls JARVIS's /llm/v1 endpoint, which answers with `claude -p` (hub/llm.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets as pysecrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..events import spawn

LABELS = {"openclaw": "OpenClaw", "hermes": "Hermes"}
KEYS = {"openclaw": "hub-openclaw-token", "hermes": "hub-hermes-key", "llm": "hub-llm-key", "research": "hub-research-key",
        "telegram": "hub-openclaw-telegram"}
KEY_LABELS = {"openclaw": "JARVIS: токен шлюза OpenClaw", "hermes": "JARVIS: ключ Hermes",
              "llm": "JARVIS: ключ модели для Hermes", "research": "JARVIS: ключ поиска для агентов",
              "telegram": "JARVIS: бот Telegram в OpenClaw"}


def dev_home() -> bool:
    return bool(os.environ.get("JARVIS_HOME"))


class Layout:
    """Where the agents and their data are, for one JARVIS home."""

    def __init__(self, config, paths) -> None:
        self.cfg, self.paths = config, paths

    @property
    def agents(self) -> Path:
        return Path(os.path.expanduser(self.cfg.agents_dir)) if self.cfg.agents_dir else self.paths.data / "agents"

    @property
    def shared_skills(self) -> Path:
        return self.paths.data / "skills-shared"

    @property
    def hub_state(self) -> Path:
        return self.paths.state / "hub"

    def binary(self, name: str) -> Path:
        if name == "openclaw":
            return self.agents / "openclaw" / "current" / "node_modules" / ".bin" / "openclaw"
        return self.agents / "hermes" / "current" / "bin" / "hermes"

    @property
    def node_bin(self) -> Path:
        return self.agents / "node" / "current" / "bin"

    @property
    def openclaw_state(self) -> Path:
        if self.cfg.openclaw_state_dir:
            return Path(os.path.expanduser(self.cfg.openclaw_state_dir))
        return self.paths.data / "openclaw-state" if dev_home() else Path.home() / ".openclaw"

    @property
    def hermes_home(self) -> Path:
        if self.cfg.hermes_home:
            return Path(os.path.expanduser(self.cfg.hermes_home))
        return self.paths.data / "hermes-home" if dev_home() else Path.home() / ".hermes"

    def installed(self, name: str) -> bool:
        return self.binary(name).exists()

    def version(self, name: str) -> str:
        cur = self.agents / name / "current"
        try:
            return os.readlink(cur)
        except OSError:
            return ""


def ensure_key(store, name: str) -> str:
    """A key JARVIS and one agent share; made once, kept in the keyring (never in a file)."""
    value = store.get(KEYS[name])
    if not value:
        value = pysecrets.token_hex(24)   # 48 characters: Hermes requires at least 16
        try:
            store.store(KEYS[name], value, KEY_LABELS[name])
        except (RuntimeError, ValueError, OSError):
            # No keyring (tests, a bare session): keep it for this run only — the agent gets it in its environment.
            _EPHEMERAL[name] = _EPHEMERAL.get(name) or value
            return _EPHEMERAL[name]
    return value


_EPHEMERAL: dict[str, str] = {}


def key(store, name: str) -> str | None:
    return store.get(KEYS[name]) or _EPHEMERAL.get(name)


def clean_env() -> dict[str, str]:
    """The core's environment without anything that looks like a key: an agent gets only the keys JARVIS hands it."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "_PASSWORD"))}
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1,localhost"]))
    return env


def launch_spec(name: str, cfg, layout: Layout, store) -> tuple[list[str], dict[str, str]]:
    """The command and environment that run one agent's service in the foreground."""
    env = clean_env()
    if name == "openclaw":
        env["PATH"] = f"{layout.node_bin}:{env.get('PATH', '')}"
        env["OPENCLAW_GATEWAY_TOKEN"] = ensure_key(store, "openclaw")
        env["OPENCLAW_STATE_DIR"] = str(layout.openclaw_state)
        env["OPENCLAW_CONFIG_PATH"] = str(layout.openclaw_state / "openclaw.json")
        tg = store.get(KEYS["telegram"])    # set by «подключи Telegram» in the hub (Hub.connect_telegram)
        if tg:
            env["TELEGRAM_BOT_TOKEN"] = tg
        if cfg.clawhub_url:
            env["OPENCLAW_CLAWHUB_URL"] = cfg.clawhub_url
        argv = [str(layout.binary("openclaw")), "gateway", "run", "--bind", "loopback", "--auth", "token",
                "--port", str(cfg.openclaw_port), "--force"]   # --force: a gateway left by a crashed core is replaced
    else:
        env["HERMES_HOME"] = str(layout.hermes_home)
        env.update(API_SERVER_ENABLED="true", API_SERVER_HOST="127.0.0.1", API_SERVER_PORT=str(cfg.hermes_port),
                   API_SERVER_KEY=ensure_key(store, "hermes"), JARVIS_LLM_KEY=ensure_key(store, "llm"))
        argv = [str(layout.binary("hermes")), "gateway", "run", "--external-supervisor", "--replace"]
    return argv, env


# ---------------------------------------------------------------- one-time settings

def record_migration(layout: Layout, text: str) -> None:
    path = layout.paths.data / "hub" / "migrations.log"      # per JARVIS home (a dev core logs into its own)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")
    except OSError:
        pass


def research_bridge(layout: Layout) -> Path:
    return layout.hub_state / "research-bridge.json"


def research_mcp(layout: Layout) -> dict:
    """The MCP server that gives an agent JARVIS's fast search: the same bridge Claude uses (jarvis/mcp_bridge.py),
    with a file that names only web_search and its own key. It runs as the person and talks only to the core."""
    return {"command": sys.executable, "args": ["-m", "jarvis.mcp_bridge", str(research_bridge(layout))]}


def wanted(name: str, cfg, layout: Layout) -> dict:
    """The settings JARVIS needs in an agent's own config (compared by digest, applied only when they change)."""
    share = bool(cfg.hub_share_research and cfg.research_enabled)
    if name == "openclaw":
        return {"gateway.mode": "local", "gateway.auth.mode": "token", "gateway.bind": "loopback",
                "gateway.port": cfg.openclaw_port, "skills.load.extraDirs+": str(layout.shared_skills),
                "mcp.servers.jarvis_research": ({**research_mcp(layout), "enabled": True, "requestTimeoutMs": 120000}
                                                if share else None),
                "agents.defaults.model.primary?": cfg.openclaw_model,
                # No recurring heartbeat turn (each one is a Claude call on the person's subscription, every 30–60 min);
                # JARVIS has its own proactive notices. Only when the person set no cadence of their own.
                "agents.defaults.heartbeat.every?": "0m"}
    toolsets = list(cfg.hermes_toolsets) + (["jarvis_research"] if share else [])
    return {"toolsets": toolsets, "shared": str(layout.shared_skills), "model": cfg.hermes_model,
            "llm_url": f"http://127.0.0.1:{cfg.port}/llm/v1",
            "mcp": {**research_mcp(layout), "enabled": True} if share else None}


def digest(name: str, cfg, layout: Layout) -> str:
    return hashlib.sha256(json.dumps(wanted(name, cfg, layout), sort_keys=True).encode()).hexdigest()[:16]


HERMES_SETUP = r"""
import json, sys
from hermes_cli.config import read_raw_config, save_config
want = json.loads(sys.argv[1]); raw = read_raw_config() or {}; changed = []
pt = dict(raw.get('platform_toolsets') or {})
if pt.get('api_server') != want['toolsets']:
    pt['api_server'] = want['toolsets']; raw['platform_toolsets'] = pt; changed.append('platform_toolsets.api_server')
sk = dict(raw.get('skills') or {}); ext = list(sk.get('external_dirs') or [])
if want['shared'] not in ext:
    ext.append(want['shared']); sk['external_dirs'] = ext; raw['skills'] = sk; changed.append('skills.external_dirs')
ms = dict(raw.get('mcp_servers') or {})
if want['mcp'] and ms.get('jarvis_research') != want['mcp']:
    ms['jarvis_research'] = want['mcp']; raw['mcp_servers'] = ms; changed.append('mcp_servers.jarvis_research')
elif not want['mcp'] and (ms.get('jarvis_research') or {}).get('enabled', False):
    ms['jarvis_research'] = dict(ms['jarvis_research'], enabled=False); raw['mcp_servers'] = ms; changed.append('mcp_servers.jarvis_research off')
# JARVIS's Claude as Hermes's model: a named provider whose key comes from the environment (key_env), never the file.
prov = dict(raw.get('providers') or {})
entry = {'name': 'JARVIS (Claude)', 'base_url': want['llm_url'], 'key_env': 'JARVIS_LLM_KEY',
         'default_model': want['model'], 'api_mode': 'chat_completions'}
if prov.get('jarvis') != entry:
    prov['jarvis'] = entry; raw['providers'] = prov; changed.append('providers.jarvis')
m = dict(raw.get('model') or {})
if not m.get('provider') or m.get('provider') == 'jarvis':
    if m.get('provider') != 'jarvis' or m.get('default') != want['model']:
        m.update(provider='jarvis', default=want['model']); raw['model'] = m; changed.append('model')
if changed:
    save_config(raw, merge_existing=True)
print(json.dumps(changed))
"""


def setup(name: str, cfg, layout: Layout, store, *, run=subprocess.run) -> list[str]:
    """Bring an agent's own config to what JARVIS needs, through its own config tools. Idempotent; returns what
    changed (also appended to agents/migrations.log)."""
    argv, env = launch_spec(name, cfg, layout, store)
    layout.shared_skills.mkdir(parents=True, exist_ok=True)
    want = wanted(name, cfg, layout)
    changes: list[str] = []
    if name == "openclaw":
        layout.openclaw_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        oc = argv[0]
        path = Path(env["OPENCLAW_CONFIG_PATH"])
        try:
            current = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            current = None      # hand-edited JSON5: read key by key through its CLI instead

        def get(k: str):
            if current is not None:
                node: Any = current
                for part in k.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                return node
            r = run([oc, "config", "get", k, "--json"], env=env, capture_output=True, text=True, timeout=60)
            try:
                return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
            except ValueError:
                return None

        patch: dict = {}

        def put(k: str, value) -> None:
            node = patch
            parts = k.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            changes.append(f"openclaw {k} = {json.dumps(value, ensure_ascii=False)}")

        for k, value in want.items():
            if k.endswith("+"):                       # a list that must contain the value
                k = k[:-1]
                items = get(k) or []
                if value not in items:
                    put(k, list(items) + [value])
            elif k.endswith("?"):                     # only when the person has none of their own
                k = k[:-1]
                if value and not get(k):
                    put(k, value)
            elif k == "mcp.servers.jarvis_research":
                entry = get(k)
                if value is not None:
                    if not isinstance(entry, dict) or any(entry.get(x) != v for x, v in value.items()):
                        put(k, value)
                elif isinstance(entry, dict) and entry.get("enabled", True):
                    put(k + ".enabled", False)
            elif get(k) != value:
                put(k, value)
        if patch:
            # One validated write through OpenClaw's own config tool (objects merge, arrays replace).
            r = run([oc, "config", "patch", "--stdin"], input=json.dumps(patch), env=env, capture_output=True,
                    text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"openclaw config patch: {(r.stderr or r.stdout).strip()[-400:]}")
    else:
        layout.hermes_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        py = Path(argv[0]).with_name("python")
        r = run([str(py), "-I", "-c", HERMES_SETUP, json.dumps(want)], env=env, capture_output=True, text=True,
                timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"Hermes config: {(r.stderr or r.stdout).strip()[-400:]}")
        try:
            changed = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            changed = []
        changes += [f"hermes {c}" for c in changed]
    for c in changes:
        record_migration(layout, c)
    return changes


# ---------------------------------------------------------------- supervision

def agent_pids(name: str, layout: Layout) -> list[int]:
    """This JARVIS home's gateway processes of one agent, found by the state folder in their environment. OpenClaw's
    launcher re-spawns the real gateway in a session of its own, so its process group alone does not reach it; a core
    that was killed without its cleanup leaves one behind, and the next one would refuse to start."""
    key, want = (("OPENCLAW_STATE_DIR", str(layout.openclaw_state)) if name == "openclaw"
                 else ("HERMES_HOME", str(layout.hermes_home)))
    needle = f"{key}={want}".encode()
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            if name not in cmd or "gateway" not in cmd:
                continue
            if needle in (d / "environ").read_bytes().split(b"\0"):
                out.append(int(d.name))
        except OSError:
            continue
    return out


def terminate(pids: list[int], grace: float = 10.0) -> int:
    """SIGTERM, then SIGKILL whatever is still alive after `grace` seconds. Returns how many there were."""
    alive = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            alive.append(pid)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = [p for p in alive if Path(f"/proc/{p}").exists() and not _zombie(p)]
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(pids)


async def _port_open(port: int) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z"
    except (OSError, IndexError):
        return True


class Child:
    """An agent run as the core's child: restarted with backoff, stopped with SIGTERM and, after a grace period,
    SIGKILL (its whole process group: OpenClaw runs several processes)."""

    BACKOFF = (1, 3, 10, 30, 60)

    def __init__(self, name: str, argv: list[str], env: dict, log_path: Path) -> None:
        self.name, self.argv, self.env, self.log_path = name, argv, env, log_path
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.started = 0.0
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = spawn(self._run(), f"child:{self.name}")

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.started = time.monotonic()
            with self.log_path.open("ab") as out:
                self.proc = await asyncio.create_subprocess_exec(*self.argv, env=self.env, stdout=out, stderr=out,
                                                                 stdin=subprocess.DEVNULL, start_new_session=True)
                self.last_exit = await self.proc.wait()
            if self._stopping:
                return
            if time.monotonic() - self.started > 300:
                attempt = 0
            delay = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
            attempt += 1
            self.restarts += 1
            await asyncio.sleep(delay)

    async def stop(self, grace: float = 15) -> None:
        self._stopping = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), grace)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class Backends:
    """Both agents' clients, their supervision and health. Owned by the hub (hub/__init__.py)."""

    HEALTH_S = 30

    def __init__(self, config, paths, db, bus, audit, store) -> None:
        self.cfg, self.paths, self.db, self.bus, self.audit, self.store = config, paths, db, bus, audit, store
        self.layout = Layout(config, paths)
        self.openclaw = None
        self.hermes = None
        self.children: dict[str, Child] = {}
        self.health: dict[str, dict] = {}
        self.setup_error: dict[str, str] = {}
        self.lockdown = False                 # «стоп всем агентам»: their runs stop, new ones are refused until lifted
        self._task: asyncio.Task | None = None
        self._listeners: list = []
        self.last_used: dict[str, float] = {}
        self._waking: dict[str, asyncio.Future] = {}

    def enabled(self, name: str) -> bool:
        return bool(getattr(self.cfg, f"{name}_enabled"))

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    async def start(self) -> None:
        from .hermes import Hermes
        from .openclaw import GatewayClient, OpenClaw
        if self.enabled("openclaw"):
            client = GatewayClient(f"ws://127.0.0.1:{self.cfg.openclaw_port}", token=lambda: key(self.store, "openclaw"))
            self.openclaw = OpenClaw(client)
            for event in ("skills.changed", "plugins.changed", "jarvis.state", "health", "cron", "shutdown"):
                client.on(event, self._openclaw_event)
            if not self.on_demand("openclaw"):
                await self._ensure("openclaw")
                client.start()
            else:
                self.health["openclaw"] = self._sleeping("openclaw")
        if self.enabled("hermes"):
            self.hermes = Hermes(f"http://127.0.0.1:{self.cfg.hermes_port}", key=lambda: key(self.store, "hermes"),
                                 home=self.layout.hermes_home)
            await self._ensure("hermes")
        self._task = spawn(self._health_loop(), "hub-health")

    # ---------------------------------------------------------------- on demand (OpenClaw: ~1.5 GB resident)
    @property
    def keep_openclaw(self) -> bool:
        return bool(self.db.get_kv("hub_keep_openclaw", False))

    def set_keep(self, keep: bool) -> None:
        self.db.set_kv("hub_keep_openclaw", bool(keep))

    def on_demand(self, name: str) -> bool:
        return name == "openclaw" and bool(self.cfg.openclaw_on_demand) and not self.keep_openclaw

    def running(self, name: str) -> bool:
        child = self.children.get(name)
        return child is not None and child.running

    def _sleeping(self, name: str) -> dict:
        return {"name": name, "label": LABELS[name], "state": "sleeping", "healthy": False, "error": "",
                "version": self.layout.version(name)}

    def touch(self, name: str) -> None:
        self.last_used[name] = time.monotonic()

    async def wake(self, name: str, timeout: float = 90) -> bool:
        """Start an on-demand agent (if needed) and wait until it answers. Concurrent callers share one start."""
        if not self.enabled(name) or not self.layout.installed(name) or self.lockdown:
            return False
        self.touch(name)
        if name == "openclaw" and self.openclaw is not None and self.openclaw.c.connected:
            return True
        if name == "hermes" and self.hermes is not None and self.hermes.state == "connected":
            return True
        fut = self._waking.get(name)
        if fut is None:
            fut = self._waking[name] = asyncio.ensure_future(self._wake(name, timeout))
            fut.add_done_callback(lambda _f: self._waking.pop(name, None))
        return await asyncio.shield(fut)

    async def _wake(self, name: str, timeout: float) -> bool:
        self.health[name] = {**self._sleeping(name), "state": "starting"}
        self.bus.publish("hub.backends", {"status": self.snapshot()})
        await self._ensure(name)
        ok = False
        if name == "openclaw" and self.openclaw is not None:
            # Connect once the port is open: retrying earlier only waits out the client's backoff (measured: 23 s → ~11 s).
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not await _port_open(self.cfg.openclaw_port):
                await asyncio.sleep(0.25)
            self.openclaw.c.start()
            ok = await self.openclaw.c.wait_ready(max(1.0, deadline - time.monotonic()))
        elif name == "hermes" and self.hermes is not None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not ok:
                ok = (await self.hermes.status())["healthy"]
                if not ok:
                    await asyncio.sleep(0.5)
        await self.refresh(name)
        self.audit.write("hub.woken", agent=name, ok=ok)
        return ok

    async def sleep(self, name: str) -> None:
        """Stop an on-demand agent that has been quiet (its data and settings stay)."""
        if name == "openclaw" and self.openclaw is not None:
            await self.openclaw.c.close()
        child = self.children.pop(name, None)
        if child is not None:
            await child.stop()
        left = await asyncio.to_thread(agent_pids, name, self.layout)
        if left:
            await asyncio.to_thread(terminate, left, 5.0)
        self.health[name] = self._sleeping(name)
        self.bus.publish("hub.backends", {"status": self.snapshot()})
        self.audit.write("hub.slept", agent=name)

    def busy(self, name: str) -> bool:
        b = self.openclaw if name == "openclaw" else self.hermes
        return bool(b is not None and b.runs) or name in self._waking

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.openclaw is not None:
            await self.openclaw.c.close()
        if self.hermes is not None:
            await self.hermes.close()
        await asyncio.gather(*(child.stop() for child in self.children.values()), return_exceptions=True)
        for name in list(self.children):        # the gateway the launcher started in a session of its own
            left = await asyncio.to_thread(agent_pids, name, self.layout)
            if left:
                await asyncio.to_thread(terminate, left, 5.0)

    async def _ensure(self, name: str) -> None:
        """Make sure the service runs. Never installs anything; applies JARVIS's settings when they changed."""
        if not self.layout.installed(name):
            self.health[name] = {"name": name, "label": LABELS[name], "state": "not_installed", "healthy": False,
                                 "error": "не установлен (scripts/install-agents.sh)"}
            return
        want = digest(name, self.cfg, self.layout) + ":" + self.layout.version(name)
        if self.db.get_kv(f"hub_setup_{name}") != want:
            try:
                changes = await asyncio.to_thread(setup, name, self.cfg, self.layout, self.store)
                self.db.set_kv(f"hub_setup_{name}", want)
                self.setup_error.pop(name, None)
                if changes:
                    self.audit.write("hub.setup", agent=name, changes=changes)
            except Exception as exc:   # noqa: BLE001 — it still starts; the hub shows the error
                self.setup_error[name] = str(exc)[:300]
                self.audit.write("hub.setup_failed", agent=name, error=str(exc)[:300])
        if name not in self.children:
            stale = await asyncio.to_thread(agent_pids, name, self.layout)
            if stale:                       # left by a core that did not stop cleanly: it holds the state folder
                await asyncio.to_thread(terminate, stale)
                self.audit.write("hub.stale_stopped", agent=name, pids=stale)
            argv, env = launch_spec(name, self.cfg, self.layout, self.store)
            child = Child(name, argv, env, self.paths.state / "logs" / f"{name}.log")
            self.children[name] = child
            child.start()
            self.audit.write("hub.started", agent=name)

    async def restart(self, name: str) -> dict:
        if name in self.children:
            await self.children.pop(name).stop()
            left = await asyncio.to_thread(agent_pids, name, self.layout)
            if left:
                await asyncio.to_thread(terminate, left, 5.0)
        await self._ensure(name)
        self.touch(name)
        if name == "openclaw" and self.openclaw is not None:
            self.openclaw.c.reconnects = 0
            self.openclaw.c.start()
        return {"ok": True, "message": f"{LABELS[name]} перезапущен"}

    def _openclaw_event(self, frame: dict) -> None:
        event = frame.get("event", "")
        if event in ("skills.changed", "plugins.changed"):
            for fn in self._listeners:
                fn("openclaw", event)
        elif event == "jarvis.state":
            spawn(self.refresh("openclaw"), "hub-refresh-openclaw")

    async def refresh(self, name: str) -> dict:
        backend = self.openclaw if name == "openclaw" else self.hermes
        if backend is not None and self.on_demand(name) and not self.running(name) and name not in self._waking:
            st = self._sleeping(name)
        elif backend is None:
            st = {"name": name, "label": LABELS[name], "state": "off", "healthy": False, "error": ""}
        elif not self.layout.installed(name):
            st = {"name": name, "label": LABELS[name], "state": "not_installed", "healthy": False,
                  "error": "не установлен (scripts/install-agents.sh)"}
        else:
            try:
                st = await backend.status()
            except Exception as exc:   # noqa: BLE001
                st = {"name": name, "label": LABELS[name], "state": "error", "healthy": False, "error": str(exc)[:200]}
            child = self.children.get(name)
            if not st.get("healthy") and child is not None and child.running and time.monotonic() - child.started < 90:
                st["state"] = "starting"
        st["version"] = st.get("version") or self.layout.version(name)
        if name in self.setup_error:
            st["setup_error"] = self.setup_error[name]
        child = self.children.get(name)
        if child is not None:
            st["restarts"] = child.restarts
            st["pid"] = child.proc.pid if child.running and child.proc is not None else None
        before = self.health.get(name, {})
        self.health[name] = st
        if (before.get("state"), before.get("healthy")) != (st.get("state"), st.get("healthy")):
            self.bus.publish("hub.backends", {"status": self.snapshot()})
            for fn in self._listeners:
                fn(name, "health")
        return st

    async def _health_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            for name in ("openclaw", "hermes"):
                if not self.enabled(name):
                    continue
                try:
                    if self.on_demand(name) and not self.running(name):
                        if self.health.get(name, {}).get("state") != "sleeping":
                            self.health[name] = self._sleeping(name)
                        continue
                    await self.refresh(name)
                    quiet = time.monotonic() - self.last_used.get(name, 0)
                    if (self.on_demand(name) and self.running(name) and not self.busy(name)
                            and self.cfg.agents_idle_min and quiet > self.cfg.agents_idle_min * 60):
                        await self.sleep(name)
                except Exception:   # noqa: BLE001 — health is best effort
                    pass
            awake = [h for n, h in self.health.items() if h.get("state") != "sleeping"]
            await asyncio.sleep(self.HEALTH_S if all(h.get("healthy") for h in awake) else 5)

    def snapshot(self) -> dict:
        out: dict[str, Any] = {}
        for name in ("openclaw", "hermes"):
            out[name] = dict(self.health.get(name) or {"name": name, "label": LABELS[name], "healthy": False,
                                                       "state": "starting" if self.enabled(name) else "off"})
            out[name]["installed"] = self.layout.installed(name)
            out[name]["enabled"] = self.enabled(name)
            out[name]["on_demand"] = self.on_demand(name)
        out["keep_openclaw"] = self.keep_openclaw
        out["idle_min"] = self.cfg.agents_idle_min
        out["lockdown"] = self.lockdown
        return out

    async def abort_runs(self) -> dict:
        stopped = {"openclaw_runs": 0, "hermes_runs": 0}
        if self.openclaw is not None and self.openclaw.c.connected:
            stopped["openclaw_runs"] = await self.openclaw.abort_all()
        if self.hermes is not None:
            stopped["hermes_runs"] = await self.hermes.stop_all()
        return stopped

    async def stop_all(self) -> dict:
        """Emergency stop for the agents: their runs stop and new ones are refused until the person lifts it."""
        self.lockdown = True
        stopped = await self.abort_runs()
        self.bus.publish("hub.backends", {"status": self.snapshot()})
        self.audit.write("hub.stop_all", **stopped)
        return stopped

    def lift(self) -> None:
        self.lockdown = False
        self.bus.publish("hub.backends", {"status": self.snapshot()})
        self.audit.write("hub.resumed")
