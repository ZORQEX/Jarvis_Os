"""Is the microphone delivering sound at its real speed?

Measured 2026-10-26 on the user's ASUS laptop (Intel Alder Lake HDA + Realtek ALC256, digital internal mic): after the
laptop woke from a 9-hour sleep the capture side ran at ~8 200 samples/s instead of 48 000 (speakers were fine, a direct
ALSA read failed with «Input/output error»). The browser then handed JARVIS a garbled recording and every phrase came
back empty — the window showed «слушаю» and nothing followed. Three earlier wake-ups that week were fine: intermittent.

What does not help (tried live the same day): re-binding the sound driver as root (full re-probe of the codec, still
0.18 of real time); the controller has no PCI reset; its own idle power-down (D3hot) neither. What is left is a reboot,
so JARVIS says exactly that and offers it (power_action, always asks)."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal

RATE = 16000
BROKEN_BELOW = 0.6         # healthy capture measures 0.95–1.05; the broken one measured 0.17–0.18
OK_FROM = 0.85


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
