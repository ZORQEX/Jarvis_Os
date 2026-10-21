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
    # Claude Code as the computer/code executor: empty model = the CLI's own default
    claude_model: str = ""
    claude_budget_usd: float = 5.0          # per Claude Code job: a whole site or app needs more than an edit
    claude_timeout_s: float = 1800.0        # a whole site or app can take a while
    code_workspaces: list[str] = field(default_factory=lambda: ["~/Projects"])
    # Claude Code may work in any folder of the home directory (the user asked for it), never in keys, passwords,
    # browser profiles, sessions or JARVIS's own data (SECRET_PATHS) — those are also denied to its file tools.
    claude_home_wide: bool = True
    max_tool_rounds: int = 12          # tool calls Claude may make for one request
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
    screenshot_dir: str = "~/Pictures/Jarvis"
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
