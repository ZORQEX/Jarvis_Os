"""Is the microphone delivering sound at its real speed, and the restart of the sound card when it is not.

Measured 2026-10-26 on the user's ASUS laptop (Intel Alder Lake HDA + Realtek ALC256, digital internal mic): after the
laptop woke from a 9-hour sleep the capture side ran at ~8 200 samples/s instead of 48 000 (speakers were fine, a direct
ALSA read failed with «Input/output error»). The browser then handed JARVIS a garbled recording and every phrase came
back empty — the window showed «слушаю» and nothing followed. Three earlier wake-ups that week were fine, so it is
intermittent. Only re-binding the sound driver (root) or a reboot brings the mic back; `restart_argv` builds the
re-bind for pkexec (the system asks the person for the password)."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from pathlib import Path

RATE = 16000
BROKEN_BELOW = 0.6         # healthy capture measures 0.95–1.05; the broken one measured 0.17
OK_FROM = 0.85
PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
DRIVER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SYS = Path("/sys")


async def capture_speed(seconds: float = 1.5, wait_first: float = 3.0, argv: list[str] | None = None) -> float | None:
    """Seconds of sound the default microphone delivers per second of wall time (≈1.0 when healthy, 0.0 when it gives
    nothing at all). None: nothing to measure with (no pw-record). Counting starts at the first chunk: opening a
    sleeping device takes ~0.6 s and that chunk may hold buffered sound."""
    argv = argv or ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"]
    if not shutil.which(argv[0]):
        return None
    loop = asyncio.get_running_loop()
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                    start_new_session=True)
    except OSError:
        return None
    first, got = None, 0
    try:
        end = loop.time() + wait_first
        while (left := end - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(3200), left)
            except TimeoutError:
                break
            if not chunk:
                break
            if first is None:
                first = loop.time()
                end = first + seconds
                continue
            got += len(chunk)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    if first is None:
        return 0.0
    spent = loop.time() - first
    return round(got / 2 / RATE / spent, 3) if spent > 0.2 else None


def broken(speed: float | None) -> bool:
    return speed is not None and speed < BROKEN_BELOW


def sound_card(card: int) -> tuple[str, str] | None:
    """(PCI address, driver) of ALSA card `card`, e.g. ("0000:00:1f.3", "snd_hda_intel"); None if it is not a PCI
    audio device (USB headsets are not re-bound: unplugging them does the same)."""
    dev = SYS / "class" / "sound" / f"card{int(card)}" / "device"
    try:
        addr = dev.resolve().name
        driver = (dev / "driver").resolve().name
        cls = (dev / "class").read_text().strip()
    except OSError:
        return None
    if not (PCI_RE.match(addr) and DRIVER_RE.match(driver) and cls.startswith("0x0403")):
        return None
    if not (SYS / "bus" / "pci" / "drivers" / driver / addr).exists():
        return None
    return addr, driver


async def default_card() -> int | None:
    """ALSA card number of the default microphone (wpctl)."""
    if not shutil.which("wpctl"):
        return None
    proc = await asyncio.create_subprocess_exec("wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except TimeoutError:
        proc.kill()
        return None
    m = re.search(r'\b(?:api\.alsa\.pcm\.card|alsa\.card) = "(\d+)"', out.decode("utf-8", "replace"))
    return int(m.group(1)) if m else None


def restart_argv(addr: str, driver: str) -> list[str]:
    """Re-bind the sound driver as root through pkexec (one password prompt). Address and driver go in as arguments,
    never into the script text."""
    if not (PCI_RE.match(addr) and DRIVER_RE.match(driver)):
        raise ValueError("bad sound card address")
    script = 'd="/sys/bus/pci/drivers/$2"; echo "$1" > "$d/unbind" && sleep 1 && echo "$1" > "$d/bind"'
    return ["pkexec", "/bin/sh", "-c", script, "jarvis-mic", addr, driver]
