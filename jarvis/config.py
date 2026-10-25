"""Paths and configuration.

All locations follow XDG. Setting JARVIS_HOME puts every directory under one
root, which the tests use to stay isolated from the real user profile.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or (HOME / default))


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    state: Path

    @classmethod
    def detect(cls) -> "Paths":
        root = os.environ.get("JARVIS_HOME")
        if root:
            r = Path(root)
            return cls(r / "config", r / "data", r / "state")
        return cls(
            _xdg("XDG_CONFIG_HOME", ".config") / "jarvis",
            _xdg("XDG_DATA_HOME", ".local/share") / "jarvis",
            _xdg("XDG_STATE_HOME", ".local/state") / "jarvis",
        )

    def ensure(self) -> None:
        for p in (self.config, self.data, self.state, self.artifacts):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def db(self) -> Path:
        return self.data / "jarvis.db"

    @property
    def artifacts(self) -> Path:
        return self.data / "artifacts"

    @property
    def token_file(self) -> Path:
        return self.state / "session.token"

    @property
    def audit_log(self) -> Path:
        return self.state / "audit.jsonl"

    @property
    def server_log(self) -> Path:
        return self.state / "server.log"


def limit(seconds: float | int | None) -> float | None:
    """A configured limit → asyncio timeout (None = wait as long as it takes)."""
    return float(seconds) if seconds and seconds > 0 else None


PRIVACY_PROFILES = ("AUTHORIZED_CLOUD", "PRIVATE_LOCAL", "STRICT_OFFLINE")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8791
    # All AI runs through the local Claude Code CLI (the user's own Claude sign-in).
    # Conversation, web search, texts, documents, screenshots: a fast, capable model (measured ~1.0–1.5 s to first words).
    claude_chat_model: str = "claude-sonnet-5-5"
    # Short structured jobs (reading a message command into JSON, planning): the fastest model.
    claude_fast_model: str = "claude-haiku-5-5"
    # Spoken answers: the same conversation, switched to the fast model for voice turns. Measured 30 questions:
    # Haiku 0.9–1.9 s to first words, never stalled; Sonnet ~1.0 s typical but 4–10 s stalls in 1–4 of 10 answers.
    # Settings → Голос can switch voice back to the main model («подробнее, но иногда дольше»).
    claude_voice_model: str = "claude-haiku-5-5"
    # When a model is overloaded or unavailable, the CLI answers with the other models above (`--fallback-model`) and
    # tries the chosen one again on the next question. false = fail instead (JARVIS then says so in plain words).
    claude_fallback: bool = True
    # Claude Code as the computer/code executor: empty model = the CLI's own default
    claude_model: str = ""
    # No limits on thinking time by default: research, builds and installs take what they need and give a result.
    # «Стоп» / the Stop button / emergency stop end any of them at once. 0 = no limit.
    claude_budget_usd: float = 0.0          # per Claude Code job (0 = no cap)
    claude_timeout_s: float = 0.0           # Claude Code jobs, research, one-off steps, one conversation answer
    claude_max_turns: int = 0               # Claude Code steps per job (0 = no cap)
    code_workspaces: list[str] = field(default_factory=lambda: ["~/Projects"])
    # Claude Code may work in any folder of the home directory (the user asked for it), never in keys, passwords,
    # browser profiles, sessions or JARVIS's own data (SECRET_PATHS) — those are also denied to its file tools.
    claude_home_wide: bool = True
    max_tool_rounds: int = 0           # tool calls in one answer (0 = no cap; «стоп» ends a stuck loop)
    ai_timeout_s: float = 120.0
    piper_voice: str = "ru_RU-denis-medium"   # emergency fallback voice if Silero cannot speak
    history_turns: int = 12
    privacy_profile: str = "AUTHORIZED_CLOUD"
    approval_ttl_s: int = 900
    # Folders JARVIS may read and search. Writes are limited to write_dir.
    read_scopes: list[str] = field(default_factory=lambda: [
        "~/Documents", "~/Projects", "~/Downloads", "~/Desktop", "~/Pictures", "~/Work", "~/Music", "~/Videos",
    ])
    write_dir: str = "~/Documents/Jarvis"
    skills_dir: str = "~/.local/share/jarvis-skills"   # abilities JARVIS builds for itself (jarvis/skills.py)
    screenshot_dir: str = "~/Pictures/Jarvis"
    # «Заказы»: the fashion-orders app (fashion/, jarvis/fashion.py) — started with the core when installed.
    fashion_enabled: bool = True
    fashion_port: int = 8797
    fashion_claude_model: str = "sonnet"     # its assistant's model in Claude Code (the user's subscription)
    # Fast web search (jarvis/research.py): a local SearXNG started with the core when installed (scripts/install.sh).
    # searxng_url = an already running SearXNG to use instead (JARVIS then does not start or stop one).
    research_enabled: bool = True
    searxng_port: int = 8796
    searxng_url: str = ""
    # OpenClaw and Hermes Agent (from morph-rq/jarvis-os; jarvis/hub, ADR-008): installed by scripts/install-agents.sh,
    # started with the core as its children on loopback, each with a key JARVIS keeps in the keyring. Both think with
    # Claude through the user's own Claude Code sign-in: OpenClaw runs the `claude` program itself (claude-cli/…),
    # Hermes asks JARVIS's endpoint /llm/v1, which answers with `claude -p` (ADR-005: no API keys, no other models).
    agents_dir: str = ""                    # "" = <data>/agents (scripts/install-agents.sh)
    openclaw_enabled: bool = True
    openclaw_port: int = 18789
    openclaw_model: str = "claude-cli/claude-sonnet-5-5"
    openclaw_state_dir: str = ""            # "" = OpenClaw's own ~/.openclaw (a dev/test home gets its own)
    # OpenClaw keeps ~1.5 GB of memory resident (measured 2026-10-25): it starts when something needs it (a skill,
    # ClawHub, the panel) and stops after agents_idle_min quiet minutes — unless a channel connected through it or the
    # person's own automation must keep running, or «держать запущенным» is on in «Возможности».
    openclaw_on_demand: bool = True
    agents_idle_min: float = 20.0
    clawhub_url: str = ""                   # "" = https://clawhub.ai (read only; installs go through OpenClaw)
    hermes_enabled: bool = True
    hermes_port: int = 18642
    hermes_home: str = ""                   # "" = Hermes's own ~/.hermes (a dev/test home gets its own)
    hermes_model: str = "claude-sonnet-5-5"
    # What Hermes may use when JARVIS hands it work: no terminal, processes, code execution, cron or browser.
    hermes_toolsets: list[str] = field(default_factory=lambda: ["file", "skills", "todo", "memory", "session_search"])
    hermes_timeout_s: float = 900.0
    hub_share_research: bool = True         # OpenClaw and Hermes search through JARVIS's fast search (MCP, read only)
    hub_sync_hours: float = 12.0            # ClawHub: what is new (read only)
    hub_update_check_hours: float = 24.0
    hub_auto_update_skills: str = "trusted"  # off | trusted (only trusted, clean, nothing new asked for)
    hub_trusted: list[str] = field(default_factory=list)
    # Coding agents on this computer (Claude Code, Codex) report what they do through their hooks; their permission
    # requests can be answered in JARVIS's window (jarvis/hub/coding.py, idea from Coucou). Silence = their own prompt.
    coding_agents_enabled: bool = True
    # Spoken/typed app names -> argv. Only these can be launched.
    apps: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: Paths) -> "Config":
        cfg = cls()
        f = paths.config / "config.toml"
        if f.exists():
            raw = tomllib.loads(f.read_text("utf-8"))
            flat: dict = {}
            for v in raw.values():
                if isinstance(v, dict):
                    flat.update(v)
            for k, v in flat.items():
                if hasattr(cfg, k) and k != "apps":
                    setattr(cfg, k, v)
            if isinstance(raw.get("apps"), dict):
                cfg.apps.update({k.casefold(): list(v) for k, v in raw["apps"].items()})
        if cfg.privacy_profile not in PRIVACY_PROFILES:
            cfg.privacy_profile = "PRIVATE_LOCAL"
        return cfg

    def expand(self, p: str) -> Path:
        return Path(os.path.expanduser(p)).resolve()

    @property
    def read_roots(self) -> list[Path]:
        roots = [self.expand(p) for p in self.read_scopes]
        roots.append(self.expand(self.write_dir))
        return roots
