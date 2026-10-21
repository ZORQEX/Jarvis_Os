"""Local speech recognition (GigaAM v3 via onnx-asr) as a separate process. Nothing leaves the computer, no quota.

This is the only speech recognition in JARVIS (there is no cloud fallback)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

STT_HOME = Path(os.path.expanduser("~/.local/share/jarvis/stt"))
MODEL = "gigaam-v3-e2e-rnnt"


class LocalSTT:
    name = MODEL

    def __init__(self, home: Path = STT_HOME):
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._n = 0

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    def available(self) -> bool:
        return self.python.exists() and (self.home / "hf").exists()

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        worker = Path(__file__).with_name("stt_worker.py")
        env = {**{k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET"))},
               "HF_HOME": str(self.home / "hf"), "HF_HUB_OFFLINE": "1", "JARVIS_STT_MODEL": MODEL}
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(worker), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True, limit=1 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 120)        # model load: ~3–25 s
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError("Локальное распознавание не запустилось")

    async def transcribe(self, wav: bytes) -> str:
        if wav[:4] != b"RIFF":
            raise ValueError("локальное распознавание принимает только WAV")
        async with self.lock:
            await self.start()
            self._n += 1
            rid = f"s{self._n}"
            fd, path = tempfile.mkstemp(prefix="jarvis-stt-", suffix=".wav")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                self.proc.stdin.write((json.dumps({"id": rid, "path": path}) + "\n").encode())
                await self.proc.stdin.drain()
                while True:
                    line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
                    if not line:
                        self.proc = None
                        raise RuntimeError("Локальное распознавание остановилось")
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("id") != rid:
                        continue
                    if r.get("error"):
                        raise RuntimeError(r["error"])
                    return r.get("text") or ""
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None
