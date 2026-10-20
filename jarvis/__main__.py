"""`jarvis` command line.

  jarvis              open (or focus) the JARVIS window, starting the core if needed
  jarvis serve        run the core in the foreground
  jarvis status       print core state
  jarvis stop         emergency stop (cancel work, block consequential actions)
  jarvis resume       clear emergency stop
  jarvis say TEXT     send a command as if typed
  jarvis setup-gemini store a Gemini API key in the system keyring (hidden input)
  jarvis waybar       stream state for a Waybar custom module
"""
from __future__ import annotations

import getpass
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, Paths

WINDOW_CLASS = "jarvis-workos"
# Chromium ignores --class on Wayland and derives the app id from the host name;
# "localhost" keeps it distinct from other local web apps served on 127.0.0.1.
APP_ID = "chrome-localhost__-Default"
ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> tuple[Paths, Config]:
    p = Paths.detect()
    p.ensure()
    return p, Config.load(p)


def _token(paths: Paths) -> str | None:
    try:
        return paths.token_file.read_text().strip()
    except OSError:
        return None


def _req(cfg: Config, paths: Paths, method: str, path: str, body: dict | None = None, timeout: float = 5):
    c = http.client.HTTPConnection(cfg.host, cfg.port, timeout=timeout)
    headers = {"Host": f"127.0.0.1:{cfg.port}"}
    tok = _token(paths)
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def _alive(cfg, paths) -> bool:
    try:
        st, body = _req(cfg, paths, "GET", "/health", timeout=1)
        return st == 200 and b"jarvis-workos" in body
    except OSError:
        return False


def ensure_core(cfg: Config, paths: Paths) -> None:
    if _alive(cfg, paths):
        return
    unit = Path.home() / ".config/systemd/user/jarvis-workos.service"
    if unit.exists():
        subprocess.run(["systemctl", "--user", "start", "jarvis-workos.service"], check=False)
    else:
        log = open(paths.server_log, "ab")
        subprocess.Popen([sys.executable, "-m", "jarvis", "serve"], cwd=str(ROOT), stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if _alive(cfg, paths):
            return
        time.sleep(0.1)
    raise SystemExit(f"Ядро JARVIS не запустилось. Журнал: {paths.server_log}")


def _browser() -> str | None:
    for b in ("chromium", "google-chrome-stable", "google-chrome", "brave"):
        if shutil.which(b):
            return b
    return None


def _hypr_clients() -> list[dict]:
    if not shutil.which("hyprctl") or not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return []
    try:
        return json.loads(subprocess.run(["hyprctl", "-j", "clients"], capture_output=True, text=True,
                                         timeout=3).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def open_window(cfg: Config, paths: Paths, toggle: bool = False) -> None:
    ensure_core(cfg, paths)
    mine = [c for c in _hypr_clients() if c.get("class") in (WINDOW_CLASS, APP_ID)
            and c.get("title", "").endswith("JARVIS")]
    if mine:
        w = mine[0]
        subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{w['address']}"], capture_output=True)
        return
    b = _browser()
    if not b:
        raise SystemExit("Не найден Chromium — установите его или откройте http://127.0.0.1:%d" % cfg.port)
    url = f"http://localhost:{cfg.port}/?t={_token(paths)}"
    profile = paths.data / "browser-profile"
    argv = [b, f"--app={url}", f"--class={WINDOW_CLASS}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate,MediaRouter", "--disable-sync",
            "--password-store=basic", "--ozone-platform=wayland"]
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        # One-shot exec rules: floating, sized and centred — no change to the user's Hyprland config.
        w, h = _window_size()
        rules = f"[float; size {w} {h}; center]"
        cmd = " ".join(_q(a) for a in argv)
        subprocess.run(["hyprctl", "dispatch", "exec", f"{rules} {cmd}"], capture_output=True)
    else:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _window_size() -> tuple[int, int]:
    """1240×820 on roomy screens; otherwise ~92% of the focused monitor's usable logical area."""
    try:
        mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True,
                                         timeout=3).stdout)
        m = next((x for x in mons if x.get("focused")), mons[0])
        scale = m.get("scale") or 1
        lw, lh = m["width"] / scale, m["height"] / scale
        if m.get("transform", 0) in (1, 3, 5, 7):
            lw, lh = lh, lw
        rl, rt, rr, rb = (m.get("reserved") or [0, 0, 0, 0])[:4]
        lw, lh = lw - rl - rr, lh - rt - rb
        return int(min(1240, lw * 0.92)), int(min(820, lh * 0.92))
    except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
        return 1100, 680


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


ICONS = {"IDLE": "󰚩", "STANDBY": "󰏤", "LISTENING": "󰍬", "PROCESSING": "󰔟", "THINKING": "󰧑", "EXECUTING": "󰑮",
         "SPEAKING": "󰕾", "WAITING_APPROVAL": "󰀦", "PAUSED": "󰏤", "MUTED": "󰍭", "COMPLETED": "󰄬", "ERROR": "󰅚",
         "OFFLINE": "󰚩"}
LABELS = {"IDLE": "готов", "STANDBY": "экстренная остановка", "PROCESSING": "работаю", "THINKING": "думаю",
          "EXECUTING": "выполняю", "WAITING_APPROVAL": "нужно ваше решение", "PAUSED": "пауза",
          "COMPLETED": "готово", "ERROR": "ошибка", "OFFLINE": "не запущен"}


def waybar(cfg: Config, paths: Paths) -> None:
    """Long-running: print one JSON line per state change (Waybar custom module, return-type json)."""
    last = [None]

    def emit(state: dict | None):
        s = state["state"] if state else "OFFLINE"
        tip = f"JARVIS — {LABELS.get(s, s.lower())}"
        if state:
            if state.get("active_tasks"):
                tip += f"\nАктивных задач: {state['active_tasks']}"
            if state.get("pending_approvals"):
                tip += f"\nЖдут подтверждения: {state['pending_approvals']}"
            tip += "\nИИ: " + {"ready": "специалисты подключены", "partial": "подключены не все специалисты", "unconfigured": "не подключён"}.get(state.get("ai"),
                                                                                                state.get("ai", "?"))
        text = ICONS.get(s, "󰚩")
        if state and state.get("pending_approvals"):
            text += f" {state['pending_approvals']}"
        line = json.dumps({"text": text, "alt": s.lower(), "class": s.lower(), "tooltip": tip}, ensure_ascii=False)
        if line != last[0]:
            print(line, flush=True)
            last[0] = line

    backoff = 1.0
    while True:
        try:
            c = http.client.HTTPConnection(cfg.host, cfg.port, timeout=40)
            c.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{cfg.port}",
                                                      "Authorization": "Bearer " + (_token(paths) or "")})
            r = c.getresponse()
            if r.status != 200:
                raise OSError(r.status)
            backoff = 1.0
            while True:
                line = r.fp.readline()
                if not line:
                    raise OSError("closed")
                if line.startswith(b"data: "):
                    emit(json.loads(line[6:]))
        except (OSError, ValueError, http.client.HTTPException):
            emit(None)
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "open"
    paths, cfg = _cfg()
    if cmd == "serve":
        from .server import run
        run()
    elif cmd in ("open", "toggle"):
        open_window(cfg, paths)
    elif cmd == "status":
        if not _alive(cfg, paths):
            print("JARVIS не запущен")
            sys.exit(1)
        st, body = _req(cfg, paths, "GET", "/api/state")
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    elif cmd in ("stop", "estop"):
        st, body = _req(cfg, paths, "POST", "/api/control", {"op": "estop"})
        print(json.loads(body).get("message", body))
    elif cmd == "resume":
        st, body = _req(cfg, paths, "POST", "/api/control", {"op": "estop_clear"})
        print(json.loads(body).get("message", body))
    elif cmd == "say":
        ensure_core(cfg, paths)
        st, body = _req(cfg, paths, "POST", "/api/submit", {"text": " ".join(argv[1:]), "source": "cli"})
        print(body.decode())
    elif cmd == "setup-gemini":
        from . import secrets
        print("Ключ Gemini API будет сохранён в системном хранилище ключей (GNOME Keyring).")
        print("Получить ключ: https://aistudio.google.com/apikey  — ввод не отображается.")
        key = getpass.getpass("Ключ: ")
        secrets.store("gemini", key, "JARVIS — Gemini API key")
        print("Сохранено. Откройте Настройки → Gemini → «Проверить», чтобы проверить доступ к моделям.")
    elif cmd == "waybar":
        waybar(cfg, paths)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
