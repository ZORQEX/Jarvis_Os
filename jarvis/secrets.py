"""Credentials live in the OS secret store (GNOME Keyring via Secret Service,
`secret-tool`). They are never written to config files, the database or logs.
An environment variable is accepted for development."""
from __future__ import annotations

import os
import shutil
import subprocess
import time

SERVICE = "jarvis-workos"
_CACHE: dict[str, tuple[float, str | None]] = {}
_TTL = 10.0


def _have_tool() -> bool:
    return shutil.which("secret-tool") is not None


def get(account: str) -> str | None:
    if account == "gemini":
        env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if env:
            return env.strip()
    if not _have_tool():
        return None
    hit = _CACHE.get(account)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "account", account],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = r.stdout.strip() or None
    _CACHE[account] = (time.monotonic(), v)
    return v


def source(account: str) -> str | None:
    if account == "gemini" and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return "env"
    return "keyring" if get(account) else None


def store(account: str, value: str, label: str) -> None:
    if not _have_tool():
        raise RuntimeError("secret-tool не найден: хранилище ключей недоступно")
    value = value.strip()
    if not value or len(value) > 512 or any(c.isspace() for c in value):
        raise ValueError("Ключ имеет неверный формат")
    r = subprocess.run(["secret-tool", "store", "--label", label, "service", SERVICE, "account", account],
                       input=value, capture_output=True, text=True, timeout=30)
    _CACHE.pop(account, None)
    if r.returncode != 0:
        raise RuntimeError("Хранилище ключей отказало: " + r.stderr.strip()[:200])


def clear(account: str) -> None:
    _CACHE.pop(account, None)
    if _have_tool():
        subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", account],
                       capture_output=True, timeout=10)
