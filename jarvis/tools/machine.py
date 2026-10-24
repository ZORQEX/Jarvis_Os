"""The computer itself, the Omarchy way: processes, disk space and cleanup, health check, screen and keyboard
brightness, theme / wallpaper / do-not-disturb / night light, speakers and microphones, network, Bluetooth, power.

Ported from morph-rq/jarvis-os (capabilities/system.py, same author as the user's partner project) to JARVIS's tool
contract: one risk per tool (looking is LOW, ending a program or deleting caches is MODERATE = the person confirms,
rebooting / powering off is HIGH), argv only with timeouts, Omarchy's own helpers where they exist, and verify()
reads the real state back (brightness, theme, default device, the process gone, the space freed).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from .base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification, run_cmd

HOME = Path.home()


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}".replace(".0 ", " ") if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return f"{n:.1f} ТБ"


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").casefold().replace("ё", "е")).strip()


def best_match(query: str, items: list, key=lambda x: x) -> object | None:
    """The item whose name contains the query's words (all of them first, then most of them)."""
    q = _norm(query).split()
    if not q:
        return None
    scored = []
    for it in items:
        name = _norm(key(it))
        hits = sum(1 for w in q if w in name or (len(w) > 4 and w[:5] in name))
        if hits:
            scored.append((hits, -len(name), it))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return scored[0][2]


# ------------------------------------------------------------------ processes

# Never ended: the session would break, or JARVIS would end itself.
PROTECTED = {"systemd", "hyprland", "xwayland", "pipewire", "pipewire-pulse", "wireplumber", "dbus-daemon", "dbus-broker",
             "dbus-broker-lau", "uwsm", "hyprlock", "hypridle", "sddm", "greetd", "login", "sshd", "gnome-keyring-d",
             "xdg-desktop-por", "polkit-gnome-au", "elephant", "walker", "mako", "swayosd-server", "hyprsunset", "waybar",
             "kitty", "alacritty", "ghostty", "bash", "zsh", "fish", "claude"}


def _procs() -> dict[int, dict]:
    out: dict[int, dict] = {}
    uid = os.getuid()
    page = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            r = stat.rindex(")")
            name = stat[stat.index("(") + 1:r]
            f = stat[r + 2:].split()
            ticks, rss = int(f[11]) + int(f[12]), int(f[21]) * page
            owner = entry.stat().st_uid
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:300]
        except (OSError, ValueError, IndexError):
            continue
        out[int(entry.name)] = {"pid": int(entry.name), "name": name, "ticks": ticks, "mb": round(rss / 1048576),
                                "mine": owner == uid, "cmd": cmd}
    return out


# Programs people name, from their process names / command lines («хром» → chromium).
FRIENDLY = {"chromium": "Chromium", "chrome": "Chrome", "firefox": "Firefox", "telegram": "Telegram", "code": "VS Code",
            "obsidian": "Obsidian", "spotify": "Spotify", "steam": "Steam", "libreoffice": "LibreOffice", "soffice": "LibreOffice",
            "nautilus": "Файлы", "mpv": "mpv", "zoom": "Zoom", "discord": "Discord", "signal": "Signal", "typora": "Typora"}


JARVIS_CMD = re.compile(r"-m jarvis\b|\.local/share/jarvis|/jarvis/voice/|jarvis[-_](?:mcp|overlay)|app\.main:app|"
                        r"/jarvis/(?:browser|overlay)-profile", re.I)


def is_jarvis(cmd: str) -> bool:
    """JARVIS itself, its voice workers, its windows and the orders app — never offered for ending."""
    return bool(JARVIS_CMD.search(cmd or ""))


def _label(p: dict) -> str:
    low = (p["name"] + " " + p["cmd"].split(" ")[0]).lower()
    if is_jarvis(p["cmd"]):
        return "JARVIS"
    for k, v in FRIENDLY.items():
        if k in low:
            return v
    return p["name"]


def top_processes(sort: str = "cpu", limit: int = 8, interval: float = 0.6) -> list[dict]:
    first = _procs()
    time.sleep(interval)
    second = _procs()
    hz = os.sysconf("SC_CLK_TCK")
    # One program = all its processes (a browser runs dozens): summed, so «Chromium 2.1 ГБ» is what the person sees.
    groups: dict[str, dict] = {}
    for pid, p in second.items():
        before = first.get(pid)
        cpu = 100 * (p["ticks"] - before["ticks"]) / hz / interval if before else 0.0
        g = groups.setdefault(_label(p), {"name": _label(p), "cpu_pct": 0.0, "mb": 0, "count": 0, "mine": p["mine"]})
        g["cpu_pct"] += cpu
        g["mb"] += p["mb"]
        g["count"] += 1
    rows = sorted(groups.values(), key=lambda g: -(g["cpu_pct"] if sort == "cpu" else g["mb"]))[:limit]
    for g in rows:
        g["cpu_pct"] = round(g["cpu_pct"], 1)
    return rows


class Processes(Tool):
    spec = ToolSpec(
        id="processes", title="Что нагружает компьютер",
        description="Programs using the most CPU or memory right now (several processes of one program are summed), "
                    "or find a running program by name. Read-only.",
        params={"type": "object", "properties": {
            "sort": {"type": "string", "enum": ["cpu", "memory"]},
            "name": {"type": "string", "maxLength": 80, "description": "find this program instead of the top list"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        risk=Risk.LOW, side_effects="none", timeout_s=10, verification="Прочитано из /proc дважды с паузой")

    async def run(self, args, ctx):
        if args.get("name"):
            procs = await asyncio.to_thread(_procs)
            q = _norm(args["name"])
            found = [p for p in procs.values() if q and (q in _norm(p["name"]) or q in _norm(_label(p)))]
            mb = sum(p["mb"] for p in found)
            text = (f"{_label(found[0])}: процессов {len(found)}, памяти {mb} МБ." if found
                    else f"Программа «{args['name']}» сейчас не запущена.")
            return ToolResult({"found": [{k: p[k] for k in ("pid", "name", "mb", "mine")} for p in found[:30]]}, text)
        rows = await asyncio.to_thread(top_processes, args.get("sort", "cpu"), int(args.get("limit", 6)))
        key = "cpu_pct" if args.get("sort", "cpu") == "cpu" else "mb"
        unit = lambda r: f"{r['cpu_pct']:.0f}% процессора, {r['mb']} МБ" if key == "cpu_pct" else f"{r['mb']} МБ"
        text = ("Больше всего " + ("нагружают процессор" if key == "cpu_pct" else "занимают памяти") + ": "
                + "; ".join(f"{r['name']} — {unit(r)}" for r in rows[:5]) + ".")
        return ToolResult({"processes": rows, "sort": key}, text, card="processes")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


def alive(pid: int) -> bool:
    """Still running: a process that ended but was not yet collected by its parent (state Z) counts as ended."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2:stat.rindex(")") + 3] != "Z"


def end_targets(name: str | None, pid: int | None) -> tuple[list[dict], list[str]]:
    """The user's own processes of that program (never session, system or JARVIS ones), and what was refused."""
    procs = _procs()
    if pid:
        matches = [procs[pid]] if pid in procs else []
    else:
        q = _norm(name or "")
        matches = [p for p in procs.values() if q and (q in _norm(p["name"]) or q in _norm(_label(p)))]
    me = {os.getpid(), os.getppid()}
    ok = [p for p in matches if p["mine"] and p["pid"] not in me and p["name"].lower() not in PROTECTED
          and not is_jarvis(p["cmd"])]
    return ok, sorted({_label(p) for p in matches if p not in ok})


class ProcessEnd(Tool):
    spec = ToolSpec(
        id="process_end", title="Закрыть программу",
        description="End a running program of the user by name or pid (politely first, force only if asked). Never "
                    "system, session or JARVIS processes. The user confirms.",
        params={"type": "object", "properties": {
            "name": {"type": "string", "minLength": 2, "maxLength": 80}, "pid": {"type": "integer", "minimum": 2},
            "force": {"type": "boolean"}}},
        risk=Risk.MODERATE, side_effects="local", timeout_s=15, idempotent=False,
        verification="Процессов программы больше нет в /proc", impact="Несохранённые данные программы могут пропасть")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if not a.get("name") and not a.get("pid"):
            raise ToolInputError("Какую программу закрыть?")
        return a

    def describe(self, args):
        return f"Закрыть программу «{args.get('name') or args.get('pid')}»" + (" принудительно" if args.get("force") else "")

    async def run(self, args, ctx):
        targets, refused = await asyncio.to_thread(end_targets, args.get("name"), args.get("pid"))
        if not targets:
            raise ToolError(f"Нельзя закрыть: {', '.join(refused)} (системная или не ваша)" if refused
                            else "Такой программы сейчас нет")
        if len(targets) > 60:
            raise ToolError(f"Под это имя подходит {len(targets)} процессов — уточните название")
        pids = [p["pid"] for p in targets]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        left = set(pids)
        for _ in range(30):
            left = {p for p in left if alive(p)}
            if not left:
                break
            await asyncio.sleep(0.1)
        if left and args.get("force"):
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.sleep(0.3)
        name = _label(targets[0])
        return ToolResult({"pids": pids, "name": name}, f"Закрыл {name}.")

    async def verify(self, args, result, ctx):
        left = [p for p in result.data["pids"] if alive(p)]
        if left:
            return Verification("failed", f"Ещё работает процессов: {len(left)} — скажите «закрой принудительно»")
        return Verification("verified", self.spec.verification)


# ------------------------------------------------------------------ disk

# Only caches whose contents are safe to delete; documents are never touched.
CLEANABLE = {
    "thumbnails": ("Миниатюры картинок", HOME / ".cache/thumbnails"),
    "pip": ("Кэш pip", HOME / ".cache/pip"),
    "uv": ("Кэш uv (пакеты Python)", HOME / ".cache/uv"),
    "npm": ("Кэш npm", HOME / ".npm/_cacache"),
    "yay": ("Кэш yay (сборки AUR)", HOME / ".cache/yay"),
    "paru": ("Кэш paru (сборки AUR)", HOME / ".cache/paru/clone"),
    "go": ("Кэш сборки Go", HOME / ".cache/go-build"),
    "trash": ("Корзина", HOME / ".local/share/Trash"),
}


def _du(path: Path, budget_s: float = 4.0) -> int:
    total, deadline = 0, time.monotonic() + budget_s
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
        if time.monotonic() > deadline:
            break
    return total


def disk_usage() -> list[dict]:
    seen, rows = set(), []
    for mount in ("/", str(HOME)):
        try:
            st, u = os.statvfs(mount), shutil.disk_usage(mount)
        except OSError:
            continue
        if st.f_fsid in seen:
            continue
        seen.add(st.f_fsid)
        rows.append({"mount": mount, "total": u.total, "used": u.used, "free": u.free,
                     "used_pct": round(100 * u.used / u.total) if u.total else 0})
    return rows


def cleanup_scan() -> list[dict]:
    items = []
    for key, (title, path) in CLEANABLE.items():
        if path.is_dir() and not path.is_symlink():
            size = _du(path / "files" if key == "trash" else path)
            if size > 1_000_000:
                items.append({"item": key, "title": title, "path": str(path), "bytes": size, "size": _human(size)})
    pkg = Path("/var/cache/pacman/pkg")
    if pkg.is_dir():
        size = _du(pkg, 3)
        if size > 100_000_000:
            items.append({"item": "pacman", "title": "Старые пакеты pacman", "bytes": size, "size": _human(size),
                          "manual": "sudo paccache -rk2 (оставит 2 последние версии)"})
    return sorted(items, key=lambda i: -i["bytes"])


def _empty_dir(path: Path) -> int:
    freed = 0
    if path.is_symlink() or not path.is_dir():
        return 0
    for child in path.iterdir():
        try:
            if child.is_symlink() or child.is_file():
                freed += child.lstat().st_size
                child.unlink()
            elif child.is_dir():
                freed += _du(child, 30)
                shutil.rmtree(child)
        except OSError:
            continue
    return freed


class DiskUsage(Tool):
    spec = ToolSpec(
        id="disk_usage", title="Место на диске",
        description="Free space on / and home, and what could be cleaned (user caches, Trash, old pacman packages) "
                    "with sizes. Read-only; cleaning is disk_cleanup.",
        params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=30,
        verification="Посчитано по файловой системе")

    async def run(self, args, ctx):
        rows, items = await asyncio.gather(asyncio.to_thread(disk_usage), asyncio.to_thread(cleanup_scan))
        parts = [f"{'Система' if r['mount'] == '/' else 'Домашняя папка'}: свободно {_human(r['free'])} из "
                 f"{_human(r['total'])} (занято {r['used_pct']}%)" for r in rows]
        can = [i for i in items if not i.get("manual")]
        total = sum(i["bytes"] for i in can)
        text = ". ".join(parts) + "."
        if total:
            text += f" Можно освободить {_human(total)}: " + ", ".join(f"{i['title'][0].lower() + i['title'][1:]} {i['size']}" for i in can[:5]) \
                + ". Скажите «почисти диск», и я покажу, что удалю."
        return ToolResult({"disks": rows, "cleanable": items, "cleanable_bytes": total}, text, card="disk")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


class DiskCleanup(Tool):
    spec = ToolSpec(
        id="disk_cleanup", title="Почистить диск",
        description="Delete the contents of chosen user caches to free space: " + ", ".join(
            f"{k} ({v[0]})" for k, v in CLEANABLE.items()) + ". Without items: all caches except the Trash. Never "
            "documents or settings. The user confirms.",
        params={"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "string", "enum": sorted(CLEANABLE)}}}},
        risk=Risk.MODERATE, side_effects="local", timeout_s=180, idempotent=True,
        verification="Размер папок посчитан заново после удаления",
        impact="Удаляются только кэши (программы скачают их заново при необходимости) и, если выбрана, корзина")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        a["items"] = list(dict.fromkeys(a.get("items") or [k for k in CLEANABLE if k != "trash"]))
        return a

    def describe(self, args):
        return "Почистить: " + ", ".join(CLEANABLE[i][0][0].lower() + CLEANABLE[i][0][1:] for i in args["items"])

    async def run(self, args, ctx):
        freed = 0
        for item in args["items"]:
            path = CLEANABLE[item][1]
            if item == "trash":
                freed += await asyncio.to_thread(_empty_dir, path / "files")
                await asyncio.to_thread(_empty_dir, path / "info")
            else:
                freed += await asyncio.to_thread(_empty_dir, path)
        return ToolResult({"freed": freed, "items": args["items"]}, f"Освободил {_human(freed)}.")

    async def verify(self, args, result, ctx):
        left = 0
        for item in args["items"]:
            path = CLEANABLE[item][1]
            left += await asyncio.to_thread(_du, path / "files" if item == "trash" else path, 4)
        ok = left < 50_000_000 or left < result.data["freed"]
        return Verification("verified" if ok else "unverified", f"Осталось в этих папках {_human(left)}")


# ------------------------------------------------------------------ diagnostics

def _cpu_pct() -> int:
    with open("/proc/stat") as fh:
        a = [int(x) for x in fh.readline().split()[1:]]
    time.sleep(0.4)
    with open("/proc/stat") as fh:
        b = [int(x) for x in fh.readline().split()[1:]]
    idle, total = (b[3] + b[4]) - (a[3] + a[4]), sum(b) - sum(a)
    return round(100 * (1 - idle / total)) if total else 0


def _temps() -> list[dict]:
    rows = []
    for zone in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            name = (zone / "name").read_text().strip()
        except OSError:
            continue
        for sensor in sorted(zone.glob("temp*_input"))[:4]:
            try:
                value = int(sensor.read_text().strip()) / 1000
            except (OSError, ValueError):
                continue
            label = sensor.with_name(sensor.name.replace("_input", "_label"))
            if 0 < value < 130:
                rows.append({"sensor": f"{name}/{label.read_text().strip() if label.exists() else name}",
                             "celsius": round(value, 1)})
    return rows[:12]


def _battery() -> dict | None:
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        def read(n):
            try:
                return (bat / n).read_text().strip()
            except OSError:
                return None
        full, design = read("energy_full") or read("charge_full"), read("energy_full_design") or read("charge_full_design")
        health = round(100 * int(full) / int(design)) if full and design and int(design) else None
        return {"percent": int(read("capacity") or 0), "status": (read("status") or "").lower(), "health_pct": health,
                "cycles": int(read("cycle_count") or 0) or None}
    return None


def _failed_units() -> list[dict]:
    rows = []
    for scope in ([], ["--user"]):
        try:
            out = subprocess.run(["systemctl", *scope, "--failed", "--no-legend", "--plain", "--no-pager"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split(None, 4)
            if parts:
                rows.append({"unit": parts[0], "scope": "user" if scope else "system",
                             "description": parts[4] if len(parts) > 4 else ""})
    return rows


def _journal_errors(limit: int = 6) -> dict:
    try:
        out = subprocess.run(["journalctl", "-b", "-p", "3", "-o", "json", "--no-pager", "-n", "300"],
                             capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return {"count": None, "top": []}
    counts: dict[str, int] = {}
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode(errors="replace")
        key = f"{e.get('SYSLOG_IDENTIFIER') or e.get('_COMM') or '?'}: {str(msg)[:140]}"
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return {"count": sum(counts.values()), "top": [{"message": k, "count": v} for k, v in top]}


def _updates() -> int | None:
    if _which("checkupdates"):
        try:
            res = subprocess.run(["checkupdates"], capture_output=True, text=True, timeout=60)
            return len([x for x in res.stdout.splitlines() if x.strip()])
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def health() -> dict:
    from .system import collect
    s = collect()
    return {"cpu_pct": _cpu_pct(), "memory_pct": s["memory"]["used_pct"], "load1": s["cpu"]["load1"],
            "disks": disk_usage(), "battery": _battery(), "temperatures": _temps(), "failed_units": _failed_units(),
            "journal": _journal_errors(), "updates": _updates(), "uptime_h": round(s.get("uptime_s", 0) / 3600, 1)}


class Diagnostics(Tool):
    spec = ToolSpec(
        id="diagnostics", title="Проверка компьютера",
        description="A health check of the computer: CPU and memory load, disks, battery wear, temperatures, failed "
                    "services, errors in the system log this boot, available updates — with what looks wrong.",
        params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=70,
        verification="Прочитано из /proc, /sys, systemctl и журнала")

    async def run(self, args, ctx):
        h = await asyncio.to_thread(health)
        problems = []
        if h["memory_pct"] > 90:
            problems.append(f"память почти заполнена ({h['memory_pct']:.0f}%)")
        for d in h["disks"]:
            if d["used_pct"] > 90:
                problems.append(f"диск {d['mount']} заполнен на {d['used_pct']}%")
        if h["failed_units"]:
            problems.append(f"не запустились службы: {', '.join(u['unit'] for u in h['failed_units'][:3])}")
        hottest = max((t["celsius"] for t in h["temperatures"]), default=None)
        if hottest and hottest > 90:
            problems.append(f"сильный нагрев ({hottest:.0f}°)")
        bat = h["battery"]
        if bat and bat.get("health_pct") and bat["health_pct"] < 70:
            problems.append(f"батарея изношена: {bat['health_pct']}% от новой")
        text = f"Процессор {h['cpu_pct']}%, память {h['memory_pct']:.0f}%"
        if hottest:
            text += f", температура до {hottest:.0f}°"
        if bat:
            text += f", батарея {bat['percent']}%" + (f" (износ: осталось {bat['health_pct']}% ёмкости)"
                                                      if bat.get("health_pct") else "")
        text += ". " + ("Проблемы: " + "; ".join(problems) + "." if problems else "Всё в порядке.")
        if h["journal"].get("count"):
            text += f" В системном журнале с начала загрузки ошибок: {h['journal']['count']}."
        if h["updates"]:
            text += f" Доступно обновлений: {h['updates']} (omarchy-update)."
        return ToolResult({"health": h, "problems": problems}, text, card="health", untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


# ------------------------------------------------------------------ brightness (screen and keyboard)

def _bright_devices() -> dict[str, dict]:
    return parse_brightness(subprocess.run(["brightnessctl", "-m", "-l"], capture_output=True, text=True, timeout=5).stdout)


def parse_brightness(out: str) -> dict[str, dict]:
    devs = {}
    for line in out.splitlines():
        p = line.split(",")
        if len(p) >= 5:
            devs[p[0]] = {"id": p[0], "class": p[1], "cur": int(p[2]), "pct": int(p[3].rstrip("%")), "max": int(p[4])}
    return devs


def _pick_device(target: str) -> dict | None:
    devs = _bright_devices()
    if target == "keyboard":
        return next((d for d in devs.values() if "kbd_backlight" in d["id"]), None)
    return next((d for d in devs.values() if d["class"] == "backlight"), None)


class Brightness(Tool):
    spec = ToolSpec(
        id="brightness", title="Яркость и подсветка клавиатуры",
        description="Screen brightness or the keyboard backlight: level (0-100 %), delta (+/- %), or on/off for the "
                    "keyboard backlight.",
        params={"type": "object", "required": ["target"], "properties": {
            "target": {"type": "string", "enum": ["screen", "keyboard"]},
            "level": {"type": "integer", "minimum": 0, "maximum": 100},
            "delta": {"type": "integer", "minimum": -100, "maximum": 100},
            "on": {"type": "boolean"}}},
        risk=Risk.LOW, side_effects="local", timeout_s=6, verification="Уровень прочитан обратно (brightnessctl)")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if sum(k in a for k in ("level", "delta", "on")) != 1:
            raise ToolInputError("Укажите одно: level, delta или on")
        if not _which("brightnessctl"):
            raise ToolInputError("brightnessctl не установлен")
        return a

    def describe(self, args):
        what = "Подсветка клавиатуры" if args["target"] == "keyboard" else "Яркость экрана"
        if "on" in args:
            return f"{what}: {'включить' if args['on'] else 'выключить'}"
        return f"{what}: {args['level']}%" if "level" in args else f"{what} {args['delta']:+d}%"

    async def run(self, args, ctx):
        dev = await asyncio.to_thread(_pick_device, args["target"])
        if dev is None:
            raise ToolError("Подсветки клавиатуры на этом компьютере нет" if args["target"] == "keyboard"
                            else "Яркость экрана здесь не регулируется")
        if "on" in args:
            target = dev["max"] if args["on"] else 0
        elif "level" in args:
            target = round(dev["max"] * args["level"] / 100)
        else:
            target = round(dev["max"] * max(0, min(100, dev["pct"] + args["delta"])) / 100)
        if args["target"] == "screen" and target == 0:
            target = max(1, round(dev["max"] * 0.01))         # a black screen is not «темнее»
        if args["target"] == "keyboard" and "on" not in args and target == 0 and args.get("delta", 0) > 0:
            target = 1                                         # +10 % of a 3-step backlight is at least step 1
        r = await run_cmd(["brightnessctl", "-d", dev["id"], "set", str(target)], timeout=5)
        if r.code != 0:
            raise ToolError("brightnessctl: " + r.stderr.strip()[:200])
        pct = round(100 * target / dev["max"]) if dev["max"] else 0
        what = "Подсветка клавиатуры" if args["target"] == "keyboard" else "Яркость экрана"
        state = (f"{what} {'включена' if target else 'выключена'}" if args["target"] == "keyboard" and dev["max"] <= 3
                 else f"{what} {pct}%")
        return ToolResult({"device": dev["id"], "target": target, "max": dev["max"], "before": dev["cur"]}, state + ".")

    async def verify(self, args, result, ctx):
        dev = (await asyncio.to_thread(_bright_devices)).get(result.data["device"])
        ok = dev is not None and abs(dev["cur"] - result.data["target"]) <= max(1, dev["max"] // 100)
        return Verification("verified" if ok else "failed", f"Сейчас {dev['cur']} из {dev['max']}" if dev else "Устройство пропало")


# ------------------------------------------------------------------ look: theme, wallpaper, do-not-disturb, night light

def _themes() -> list[str]:
    return [t for t in subprocess.run(["omarchy-theme-list"], capture_output=True, text=True, timeout=8).stdout.splitlines()
            if t.strip()]


def _current_theme() -> str:
    return subprocess.run(["omarchy-theme-current"], capture_output=True, text=True, timeout=5).stdout.strip()


LIGHT = re.compile(r"light|latte|day|dawn|white|paper|solarized", re.I)


def _dnd() -> bool:
    return "do-not-disturb" in subprocess.run(["makoctl", "mode"], capture_output=True, text=True, timeout=5).stdout


def _nightlight() -> int | None:
    """The screen temperature hyprsunset applies, or None when it is not running (= normal daylight)."""
    r = subprocess.run(["hyprctl", "hyprsunset", "temperature"], capture_output=True, text=True, timeout=5)
    m = re.fullmatch(r"\s*(\d{4,5})\s*", r.stdout) if r.returncode == 0 else None
    return int(m.group(1)) if m else None


class Appearance(Tool):
    spec = ToolSpec(
        id="appearance", title="Оформление",
        description="Desktop look (Omarchy): list themes, switch theme (by name, or a light / dark one), next "
                    "wallpaper, do-not-disturb for notifications on/off, night light (warm screen) on/off.",
        params={"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["themes", "theme", "light", "dark", "wallpaper", "dnd_on", "dnd_off",
                                                  "night_on", "night_off"]},
            "theme": {"type": "string", "maxLength": 60}}},
        risk=Risk.LOW, side_effects="local", timeout_s=90, verification="Состояние прочитано обратно")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        need = {"themes": "omarchy-theme-list", "theme": "omarchy-theme-set", "light": "omarchy-theme-set",
                "dark": "omarchy-theme-set", "wallpaper": "omarchy-theme-bg-next", "dnd_on": "makoctl",
                "dnd_off": "makoctl", "night_on": "hyprctl", "night_off": "hyprctl"}[a["action"]]
        if not _which(need):
            raise ToolInputError(f"{need} не найден — это действие работает в Omarchy")
        if a["action"] == "theme" and not a.get("theme"):
            raise ToolInputError("Какую тему включить?")
        return a

    def describe(self, args):
        return {"themes": "Список тем", "theme": f"Тема «{args.get('theme', '')}»", "light": "Светлая тема",
                "dark": "Тёмная тема", "wallpaper": "Следующие обои", "dnd_on": "Не беспокоить: включить",
                "dnd_off": "Не беспокоить: выключить", "night_on": "Ночной режим экрана: включить",
                "night_off": "Ночной режим экрана: выключить"}[args["action"]]

    async def run(self, args, ctx):
        a = args["action"]
        if a in ("dnd_on", "dnd_off"):
            await run_cmd(["makoctl", "mode", "-a" if a == "dnd_on" else "-r", "do-not-disturb"], timeout=5)
            await run_cmd(["pkill", "-RTMIN+10", "waybar"], timeout=3)          # the bar's bell follows
            return ToolResult({"dnd": a == "dnd_on"}, "Уведомления не беспокоят." if a == "dnd_on"
                              else "Уведомления снова включены.")
        if a in ("night_on", "night_off"):
            temp = 4000 if a == "night_on" else 6000
            if not subprocess.run(["pgrep", "-x", "hyprsunset"], capture_output=True).stdout:
                await run_cmd(["setsid", "uwsm-app", "--", "hyprsunset"], detach=True)
                await asyncio.sleep(1)
            r = await run_cmd(["hyprctl", "hyprsunset", "temperature", str(temp)], timeout=5)
            if r.code != 0:
                raise ToolError("hyprsunset: " + (r.stderr or r.stdout).strip()[:200])
            return ToolResult({"temperature": temp}, "Ночной режим включён: экран теплее." if a == "night_on"
                              else "Ночной режим выключен.")
        if a == "wallpaper":
            r = await run_cmd(["omarchy-theme-bg-next"], timeout=20)
            if r.code != 0:
                raise ToolError("omarchy-theme-bg-next: " + r.stderr.strip()[:200])
            return ToolResult({}, "Поставил следующие обои.")
        themes, current = await asyncio.gather(asyncio.to_thread(_themes), asyncio.to_thread(_current_theme))
        if a == "themes":
            return ToolResult({"themes": themes, "current": current},
                              f"Сейчас тема {current}. Есть: {', '.join(themes)}.")
        if a in ("light", "dark"):
            pool = [t for t in themes if bool(LIGHT.search(t)) == (a == "light")]
            if not pool:
                raise ToolError(f"{'Светлой' if a == 'light' else 'Тёмной'} темы не установлено")
            target = current if current in pool else pool[0]
        else:
            target = best_match(args["theme"], themes)
            if target is None:
                raise ToolError(f"Темы «{args['theme']}» нет. Есть: {', '.join(themes)}")
        if target != current:
            r = await run_cmd(["omarchy-theme-set", target], timeout=80)
            if r.code != 0:
                raise ToolError("omarchy-theme-set: " + (r.stderr or r.stdout).strip()[:200])
        return ToolResult({"theme": target, "previous": current},
                          f"Тема {target}." + ("" if target != current else " Она уже стоит."))

    async def verify(self, args, result, ctx):
        a = args["action"]
        if a in ("dnd_on", "dnd_off"):
            ok = (await asyncio.to_thread(_dnd)) == (a == "dnd_on")
        elif a in ("night_on", "night_off"):
            ok = (await asyncio.to_thread(_nightlight)) == result.data["temperature"]
        elif a in ("theme", "light", "dark"):
            ok = (await asyncio.to_thread(_current_theme)) == result.data["theme"]
        else:
            return Verification("unverified" if a == "wallpaper" else "verified",
                                "Смена обоев не проверяется" if a == "wallpaper" else "Прочитано")
        return Verification("verified" if ok else "failed", self.spec.verification)


# ------------------------------------------------------------------ speakers and microphones

def wpctl_nodes() -> dict[str, list[dict]]:
    return parse_wpctl(subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=5).stdout)


def parse_wpctl(out: str) -> dict[str, list[dict]]:
    """Audio sinks (speakers, headphones) and sources (microphones) from `wpctl status`; `*` marks the default."""
    top, section, groups = "", "", {"sinks": [], "sources": []}
    for line in out.splitlines():
        if line and line[0] not in " │├└":
            top, section = line.strip(), ""            # «Audio», «Video», «Settings»…
            continue
        s = line.strip(" │├└─")
        if s in ("Sinks:", "Sources:"):
            section = s[:-1].lower() if top == "Audio" else ""
            continue
        if s.endswith(":"):
            section = ""
            continue
        m = re.match(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*\])?$", s)
        if section and m:
            groups[section].append({"id": int(m.group(2)), "name": m.group(3).strip(), "default": bool(m.group(1))})
    return groups


SPOKEN_DEVICES = {"наушник": ["headphone", "headset", "buds", "airpods", "наушник"],
                  "колонк": ["speaker", "колонк"], "динамик": ["speaker", "динамик", "built-in", "встроен"],
                  "hdmi": ["hdmi"], "монитор": ["hdmi", "displayport", "dp"]}


class AudioDevices(Tool):
    spec = ToolSpec(
        id="audio_devices", title="Звук: устройства",
        description="Speakers and microphones: list them, or switch the default output (speakers / headphones / HDMI) "
                    "or input (microphone) by name.",
        params={"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["list", "output", "input"]},
            "name": {"type": "string", "maxLength": 80}}},
        risk=Risk.LOW, side_effects="local", timeout_s=8, verification="Устройство по умолчанию прочитано (wpctl)")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if a["action"] != "list" and not a.get("name"):
            raise ToolInputError("На какое устройство переключить?")
        return a

    def describe(self, args):
        return "Звуковые устройства" if args["action"] == "list" else \
            f"{'Звук' if args['action'] == 'output' else 'Микрофон'}: {args['name']}"

    async def run(self, args, ctx):
        nodes = await asyncio.to_thread(wpctl_nodes)
        fmt = lambda xs: ", ".join(n["name"] + (" (сейчас)" if n["default"] else "") for n in xs) or "нет"
        if args["action"] == "list":
            return ToolResult(nodes, f"Вывод звука: {fmt(nodes['sinks'])}. Микрофоны: {fmt(nodes['sources'])}.")
        pool = nodes["sinks" if args["action"] == "output" else "sources"]
        q = _norm(args["name"])
        words = next((v for k, v in SPOKEN_DEVICES.items() if k in q), None)
        pick = next((n for n in pool if words and any(w in n["name"].lower() for w in words)), None) or \
            best_match(args["name"], pool, key=lambda n: n["name"])
        if pick is None:
            raise ToolError(f"Нет устройства «{args['name']}». Есть: {fmt(pool)}")
        r = await run_cmd(["wpctl", "set-default", str(pick["id"])], timeout=5)
        if r.code != 0:
            raise ToolError("wpctl: " + r.stderr.strip()[:200])
        return ToolResult({"id": pick["id"], "name": pick["name"], "kind": args["action"]},
                          f"{'Звук идёт' if args['action'] == 'output' else 'Микрофон'}: {pick['name']}.")

    async def verify(self, args, result, ctx):
        if args["action"] == "list":
            return Verification("verified", "Прочитано из PipeWire")
        nodes = await asyncio.to_thread(wpctl_nodes)
        pool = nodes["sinks" if args["action"] == "output" else "sources"]
        ok = any(n["id"] == result.data["id"] and n["default"] for n in pool)
        return Verification("verified" if ok else "failed", self.spec.verification)


# ------------------------------------------------------------------ network and Bluetooth

def _probe(host: str, port: int = 443, timeout: float = 3) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - t0) * 1000)
    except OSError:
        return None


def _wifi() -> dict:
    if not _which("iwctl"):
        return {}
    clean = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
    devs = clean(subprocess.run(["iwctl", "device", "list"], capture_output=True, text=True, timeout=5).stdout)
    m = re.search(r"^\s*(\w+)\s+[0-9a-f:]{17}\s+(on|off)", devs, re.M)
    if not m:
        return {}
    dev, powered = m.group(1), m.group(2) == "on"
    show = clean(subprocess.run(["iwctl", "station", dev, "show"], capture_output=True, text=True, timeout=5).stdout)
    get = lambda k: (re.search(rf"^\s*{k}\s+(.+?)\s*$", show, re.M) or [None, None])[1]
    rssi = get("RSSI") or get("AverageRSSI")
    return {"device": dev, "powered": powered, "state": get("State"), "network": get("Connected network"),
            "ip": get("IPv4 address"), "rssi": rssi}


class NetworkStatus(Tool):
    spec = ToolSpec(
        id="network_status", title="Интернет и Wi-Fi",
        description="Is the internet reachable (DNS and a connection, with latency), which Wi-Fi network the computer "
                    "is on, its signal and IP; Bluetooth adapter state. Read-only.",
        params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=15,
        verification="Проверено соединением с интернетом")

    async def run(self, args, ctx):
        def check():
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo("example.com", 443)
                dns = round((time.perf_counter() - t0) * 1000)
            except OSError:
                dns = None
            return dns, _probe("1.1.1.1"), _probe("example.com") if dns is not None else None, _wifi(), _bluetooth()
        dns, ip_ms, web_ms, wifi, bt = await asyncio.to_thread(check)
        online = ip_ms is not None or web_ms is not None
        parts = [("Интернет есть" + (f", задержка {min(x for x in (ip_ms, web_ms) if x is not None)} мс"))
                 if online else "Интернета нет" + (" (DNS не отвечает)" if dns is None else "")]
        if wifi.get("network"):
            sig = ""
            if wifi.get("rssi"):
                dbm = int(re.sub(r"[^\d-]", "", wifi["rssi"]) or 0)
                sig = ", сигнал " + ("отличный" if dbm > -55 else "хороший" if dbm > -67 else "слабый" if dbm > -80 else "очень слабый")
            parts.append(f"Wi-Fi: {wifi['network']}{sig}")
        elif wifi:
            parts.append("Wi-Fi " + ("включён, но не подключён" if wifi.get("powered") else "выключен"))
        if bt:
            parts.append("Bluetooth " + ("включён" if bt.get("on") else "выключен"))
        return ToolResult({"online": online, "dns_ms": dns, "latency_ms": ip_ms, "web_ms": web_ms, "wifi": wifi,
                           "bluetooth": bt}, ". ".join(parts) + ".")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


def _bluetooth() -> dict:
    if not _which("rfkill"):
        return {}
    out = subprocess.run(["rfkill", "-J", "list", "bluetooth"], capture_output=True, text=True, timeout=5).stdout
    try:
        devs = next(iter(json.loads(out or "{}").values()), [])
    except ValueError:
        return {}
    if not devs:
        return {}
    blocked = any(d.get("soft") == "blocked" or d.get("hard") == "blocked" for d in devs)
    return {"on": not blocked, "tools": _which("bluetoothctl")}


# ------------------------------------------------------------------ power

class PowerProfile(Tool):
    spec = ToolSpec(
        id="power_profile", title="Режим питания",
        description="The power profile: power-saver (battery lasts longer), balanced, or performance; or turn the "
                    "screens off until the mouse moves.",
        params={"type": "object", "required": ["profile"], "properties": {
            "profile": {"type": "string", "enum": ["power-saver", "balanced", "performance", "status", "screen-off"]}}},
        risk=Risk.LOW, side_effects="local", timeout_s=8, verification="Профиль прочитан обратно (powerprofilesctl)")

    def describe(self, args):
        return {"power-saver": "Режим энергосбережения", "balanced": "Сбалансированный режим питания",
                "performance": "Режим производительности", "status": "Режим питания",
                "screen-off": "Выключить экран"}[args["profile"]]

    async def run(self, args, ctx):
        p = args["profile"]
        if p == "screen-off":
            await run_cmd(["sh", "-c", "sleep 0.8; hyprctl dispatch dpms off"], detach=True)   # after the answer
            return ToolResult({"profile": p}, "Выключаю экран — пошевелите мышью, чтобы включить.")
        if not _which("powerprofilesctl"):
            raise ToolError("Режимы питания недоступны (нет power-profiles-daemon)")
        if p != "status":
            r = await run_cmd(["powerprofilesctl", "set", p], timeout=5)
            if r.code != 0:
                raise ToolError("powerprofilesctl: " + (r.stderr or r.stdout).strip()[:200])
        cur = (await run_cmd(["powerprofilesctl", "get"], timeout=5)).stdout.strip()
        names = {"power-saver": "энергосбережение", "balanced": "сбалансированный", "performance": "производительность"}
        return ToolResult({"profile": cur, "wanted": p}, f"Режим питания: {names.get(cur, cur)}.")

    async def verify(self, args, result, ctx):
        if args["profile"] in ("status", "screen-off"):
            return Verification("verified" if args["profile"] == "status" else "unverified", "Прочитано")
        cur = (await run_cmd(["powerprofilesctl", "get"], timeout=5)).stdout.strip()
        return Verification("verified" if cur == args["profile"] else "failed", f"Сейчас: {cur}")


class PowerAction(Tool):
    spec = ToolSpec(
        id="power_action", title="Сон, перезагрузка, выключение",
        description="Suspend (sleep), reboot, power off or log out of the session. Always asks first.",
        params={"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["suspend", "reboot", "poweroff", "logout"]}}},
        risk=Risk.HIGH, side_effects="local", timeout_s=10, idempotent=False,
        verification="Команда передана системе (результат — выключение сеанса)",
        impact="Открытые программы закроются (кроме сна); несохранённое может пропасть")

    def describe(self, args):
        return {"suspend": "Перевести компьютер в сон", "reboot": "Перезагрузить компьютер",
                "poweroff": "Выключить компьютер", "logout": "Выйти из сеанса"}[args["action"]]

    async def run(self, args, ctx):
        argv = {"suspend": ["systemctl", "suspend"], "reboot": ["systemctl", "reboot"],
                "poweroff": ["systemctl", "poweroff"],
                "logout": ["uwsm", "stop"] if _which("uwsm") else ["hyprctl", "dispatch", "exit"]}[args["action"]]
        # Detached and a moment later: the answer is said first, and the command ends this session anyway.
        await run_cmd(["sh", "-c", "sleep 2; exec \"$@\"", "jarvis-power", *argv], detach=True)
        return ToolResult({"action": args["action"]}, {"suspend": "Ухожу в сон.", "reboot": "Перезагружаю.",
                                                       "poweroff": "Выключаю компьютер.", "logout": "Выхожу из сеанса."}[args["action"]])

    async def verify(self, args, result, ctx):
        return Verification("unverified", self.spec.verification)


TOOLS = [Processes, ProcessEnd, DiskUsage, DiskCleanup, Diagnostics, Brightness, Appearance, AudioDevices, NetworkStatus,
         PowerProfile, PowerAction]
