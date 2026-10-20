"""Latency benchmark of local (no-AI) commands through the full Director path.
Uses an isolated JARVIS_HOME. Usage: uv run python scripts/bench.py [N]"""
import asyncio
import os
import statistics
import sys
import tempfile
import time

os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-bench-")
from jarvis.app import build  # noqa: E402

CMDS = ["состояние системы", "какие окна открыты", "найди файл смета", "который час", "Что важного по Флоре?",
        "сравни сметы по флоре"]


async def main(n: int):
    core = build()
    d = core.director
    res = {c: [] for c in CMDS}
    for _ in range(n):
        for c in CMDS:
            t0 = time.perf_counter()
            tid = await d.submit(c)
            while tid in d.rt:
                await asyncio.sleep(0.0005)
            res[c].append((time.perf_counter() - t0) * 1000)
    print(f"{'команда':32} {'p50 мс':>8} {'p95 мс':>8} {'max':>8}")
    for c, v in res.items():
        v.sort()
        print(f"{c:32} {statistics.median(v):8.1f} {v[int(len(v) * .95) - 1]:8.1f} {v[-1]:8.1f}")


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
