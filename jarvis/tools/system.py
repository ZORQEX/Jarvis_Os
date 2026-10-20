"""system_info: read-only machine status straight from /proc and /sys."""
from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path

from .base import Risk, Tool, ToolContext, ToolResult, ToolSpec, Verification


def _read(p: str | Path, default: str = "") -> str:
    try:
        return Path(p).read_text().strip()
    except OSError:
        return default


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "?"


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        out[k] = int(v.split()[0]) * 1024 if v.split() else 0
    return out


def _battery() -> dict | None:
    for d in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap = _read(d / "capacity")
        if cap:
            return {"percent": int(cap), "status": _read(d / "status", "Unknown")}
    return None


def _cpu_temp() -> float | None:
    best = None
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        typ = _read(z / "type")
        t = _read(z / "temp")
        if t.lstrip("-").isdigit() and typ in ("x86_pkg_temp", "TCPU", "cpu_thermal", "acpitz", "coretemp"):
            v = int(t) / 1000
            if typ == "x86_pkg_temp":
                return v
            best = v if best is None else max(best, v)
    return best


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def collect() -> dict:
    mem = _meminfo()
    du = shutil.disk_usage(Path.home())
    load = os.getloadavg()
    uptime = float(_read("/proc/uptime", "0").split()[0])
    omarchy = _read(Path.home() / ".local/share/omarchy/version") or None
    return {
        "hostname": platform.node(),
        "os": _os_name(),
        "omarchy": omarchy,
        "kernel": platform.release(),
        "cpu": {"model": _cpu_model(), "threads": os.cpu_count(), "load1": round(load[0], 2),
                "load_pct": round(min(100.0, load[0] / (os.cpu_count() or 1) * 100), 1),
                "temp_c": _cpu_temp()},
        "memory": {"total": mem.get("MemTotal", 0), "available": mem.get("MemAvailable", 0),
                   "used_pct": round(100 * (1 - mem.get("MemAvailable", 0) / max(1, mem.get("MemTotal", 1))), 1)},
        "disk": {"path": str(Path.home()), "total": du.total, "free": du.free,
                 "used_pct": round(100 * du.used / du.total, 1)},
        "battery": _battery(),
        "uptime_s": int(uptime),
        "collected_at": time.time(),
    }


def gib(n: int) -> str:
    return f"{n / 1024**3:.1f} ГБ".replace(".", ",")


class SystemInfo(Tool):
    spec = ToolSpec(
        id="system_info", title="Состояние системы",
        description="Read current computer status: CPU load and temperature, RAM, free disk space, battery, uptime, OS.",
        params={"type": "object", "properties": {}},
        risk=Risk.LOW, side_effects="none", timeout_s=3,
        verification="Значения прочитаны напрямую из /proc и /sys в момент запроса",
    )

    def describe(self, args):
        return "процессор, память, диск, батарея"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        d = collect()
        parts = [f"свободно {gib(d['disk']['free'])} на диске",
                 f"память занята на {d['memory']['used_pct']:.0f}%",
                 f"нагрузка процессора {d['cpu']['load_pct']:.0f}%"]
        if d["battery"]:
            parts.append(f"батарея {d['battery']['percent']}%")
        return ToolResult(d, "Система в порядке: " + ", ".join(parts) + ".", card="system")

    async def verify(self, args, result, ctx) -> Verification:
        ok = result.data["memory"]["total"] > 0 and result.data["disk"]["total"] > 0
        return Verification("verified" if ok else "failed", self.spec.verification if ok else "Пустые данные")
