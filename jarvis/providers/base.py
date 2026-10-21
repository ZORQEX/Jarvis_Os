"""Normalized AI provider contract.

Providers are capabilities of JARVIS, not separate chats. Each one declares
what it can do; the agency (jarvis/agency) decides when to use it. Secrets never
leave the provider: describe() is safe to show in the UI.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable


class Health(StrEnum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


USABLE = {Health.CONNECTED, Health.DEGRADED}


class Cap(StrEnum):
    WEB_RESEARCH = "web_research"
    VISION = "vision"
    VOICE = "voice"
    WRITING = "writing"
    TRANSLATION = "translation"
    DOC_ANALYSIS = "document_analysis"
    GENERAL = "general"            # conversation, explanations
    DESKTOP_TOOLS = "desktop_tools"  # drive JARVIS local tools by function calling
    CODE = "code"
    TERMINAL = "terminal"
    FILESYSTEM = "filesystem"
    PLANNING = "planning"


CAP_RU = {Cap.WEB_RESEARCH: "поиск в интернете", Cap.VISION: "зрение (экран, фото)", Cap.VOICE: "голос",
          Cap.WRITING: "тексты и письма", Cap.TRANSLATION: "перевод", Cap.DOC_ANALYSIS: "анализ документов",
          Cap.GENERAL: "беседа", Cap.DESKTOP_TOOLS: "управление компьютером через инструменты JARVIS",
          Cap.CODE: "код", Cap.TERMINAL: "терминал", Cap.FILESYSTEM: "файлы проекта", Cap.PLANNING: "планирование"}

SENSITIVITY = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "HIGHLY_SENSITIVE"]


def sens_rank(s: str) -> int:
    return SENSITIVITY.index(s) if s in SENSITIVITY else 3


class ProviderFailure(Exception):
    """Normalized provider error. kind: auth | quota | model | network | invalid | server | unavailable | privacy | cancelled"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.partial = False   # True if some output had already reached the user


@dataclass
class StepRequest:
    capability: Cap
    instruction: str                       # what this step must do (one clause of the objective)
    context: list[dict] = field(default_factory=list)   # [{label, text, sources}] — only what the step needs
    system: str = ""
    image: bytes | None = None
    image_mime: str = "image/png"
    workdir: str | None = None             # Claude Code
    mode: str = "inspect"                  # Claude Code: inspect | change
    permissions: list = field(default_factory=list)   # Claude Code change: subset of {"install", "download"}
    max_tokens: int = 2048
    language: str = "ru"


@dataclass
class GenResult:
    text: str
    provider: str
    model: str
    sources: list[dict] = field(default_factory=list)    # [{title, url}] or [{label, ref}]
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    meta: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


Delta = Callable[[str], None]
Progress = Callable[[str], None]


class AIProvider:
    id: str = ""
    display_name: str = ""
    role_ru: str = ""
    capabilities: set[Cap] = set()
    modalities: set[str] = set()
    cloud: bool = True              # data leaves the computer

    def configured(self) -> bool:
        raise NotImplementedError

    def health(self) -> Health:
        raise NotImplementedError

    async def health_check(self, live: bool = False) -> dict:
        raise NotImplementedError

    def model_for(self, cap: Cap) -> str:
        raise NotImplementedError

    async def generate(self, req: StepRequest, on_delta: Delta | None = None,
                       on_progress: Progress | None = None) -> GenResult:
        raise NotImplementedError

    def describe(self) -> dict:
        raise NotImplementedError


Executor = Callable[[str, dict], Awaitable[dict]]


def usage_of(u: dict) -> dict:
    """Token use of one answer. «input_tokens» counts the WHOLE context Claude read (new + cached), so the usage
    numbers in Settings show how big the conversation really is; the cached part is listed separately."""
    cached, written = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
    fresh = u.get("input_tokens")
    total = (fresh or 0) + cached + written if (fresh is not None or cached or written) else None
    return {"input_tokens": total, "output_tokens": u.get("output_tokens"),
            "cache_read_tokens": cached, "cache_write_tokens": written}
