"""Desktop companion: a tiny pinned Hyprland window (Chromium --app on /overlay) that floats above every workspace and
every application. A web page cannot move or resize its own window on Wayland, so the core does it through Hyprland's
IPC socket: dragging follows the cursor (cursorpos → movewindowpixel), expanding grows the window away from the
nearest screen edge, and the place is remembered. Coordinates are Hyprland's logical pixels.

The window gets no Hyprland animation, border or shadow (WINDOW_PROPS): with the desktop's window animation (Omarchy:
a 0.5 s «bounce») every drag step and every open/close was animated, so the companion trailed behind the cursor and
wobbled. Size, place and rounding change in one batched request, so no frame shows a half-done change."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

OVERLAY_TITLE = "JARVIS · компаньон"
ORB = 64                      # collapsed: a round window
MARGIN = 12
# Hyprland ≥ 0.53 property names; the launcher's old exec-rule names (noborder, noshadow) are ignored there.
WINDOW_PROPS = (("no_anim", 1), ("border_size", 0), ("no_shadow", 1))


def socket_path() -> Path | None:
    sig, rt = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), os.environ.get("XDG_RUNTIME_DIR")
    if not sig or not rt:
        return None
    p = Path(rt) / "hypr" / sig / ".socket.sock"
    return p if p.exists() else None


async def hypr(cmd: str) -> str:
    """One request on Hyprland's command socket (≈1 ms; no hyprctl process per frame)."""
    s = socket_path()
    if not s:
        raise RuntimeError("Hyprland недоступен")
    r, w = await asyncio.open_unix_connection(str(s))
    try:
        w.write(cmd.encode())
        await w.drain()
        return (await r.read()).decode("utf-8", "replace")
    finally:
        w.close()


async def hypr_json(cmd: str):
    return json.loads(await hypr("j/" + cmd) or "null")


async def hypr_batch(cmds: list[str]) -> str:
    """Several commands in one request: Hyprland applies them together (no frame in between)."""
    return await hypr("[[BATCH]]" + ";".join(cmds))


def prop_cmds(addr: str) -> list[str]:
    return [f"dispatch setprop address:{addr} {k} {v}" for k, v in WINDOW_PROPS]


def logical(m: dict) -> dict:
    """Monitor → its usable logical rectangle (minus Waybar and other reserved areas)."""
    scale = m.get("scale") or 1
    w, h = m["width"] / scale, m["height"] / scale
    if m.get("transform", 0) in (1, 3, 5, 7):
        w, h = h, w
    left, top, right, bottom = (m.get("reserved") or [0, 0, 0, 0])[:4]
    return {"x": m["x"] + left, "y": m["y"] + top, "w": w - left - right, "h": h - top - bottom}


def clamp(x: float, y: float, w: float, h: float, area: dict, margin: int = 4) -> tuple[int, int]:
    x = min(max(x, area["x"] + margin), area["x"] + area["w"] - w - margin)
    y = min(max(y, area["y"] + margin), area["y"] + area["h"] - h - margin)
    return round(x), round(y)


def grow(rect: dict, w: int, h: int, area: dict) -> tuple[int, int]:
    """New top-left when the window becomes w×h: it grows away from the nearest screen edges, so a companion sitting
    at the top-right opens down and to the left and stays on screen."""
    cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
    right = cx > area["x"] + area["w"] / 2
    bottom = cy > area["y"] + area["h"] / 2
    x = rect["x"] + rect["w"] - w if right else rect["x"]
    y = rect["y"] + rect["h"] - h if bottom else rect["y"]
    return clamp(x, y, w, h, area)


def default_spot(area: dict, size: int = ORB) -> tuple[int, int]:
    """Top-right corner of the usable area."""
    return round(area["x"] + area["w"] - size - MARGIN), round(area["y"] + MARGIN)


def title_for(port: int) -> str:
    """The companion of the JARVIS on the default port is «JARVIS · компаньон»; a development core on another port
    gets its own title, so it can never find and move the real one."""
    return OVERLAY_TITLE if port == 8791 else f"{OVERLAY_TITLE} :{port}"


class Overlay:
    def __init__(self, db, port: int = 8791):
        self.db = db
        self.title = title_for(port)
        self.drag: dict | None = None
        self._follow_task: asyncio.Task | None = None
        self._prepared: str | None = None        # the window address that already has WINDOW_PROPS

    async def window(self) -> dict | None:
        for c in await hypr_json("clients") or []:
            if str(c.get("title", "")) == self.title:
                if c.get("address") and c["address"] != self._prepared:
                    try:
                        await hypr_batch(prop_cmds(c["address"]))
                        self._prepared = c["address"]
                    except (OSError, RuntimeError):
                        pass
                return c
        return None

    async def area_at(self, x: float, y: float) -> dict:
        mons = await hypr_json("monitors") or []
        for m in mons:
            a = logical(m)
            full = {"x": m["x"], "y": m["y"], "w": a["w"] + (m.get("reserved") or [0, 0, 0, 0])[0] + (m.get("reserved") or [0, 0, 0, 0])[2],
                    "h": a["h"] + (m.get("reserved") or [0, 0, 0, 0])[1] + (m.get("reserved") or [0, 0, 0, 0])[3]}
            if full["x"] <= x < full["x"] + full["w"] and full["y"] <= y < full["y"] + full["h"]:
                return a
        focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
        return logical(focused) if focused else {"x": 0, "y": 0, "w": 1280, "h": 720}

    def saved(self) -> dict | None:
        v = self.db.get_kv("overlay_pos")
        return v if isinstance(v, dict) and {"x", "y"} <= set(v) else None

    def _save(self, x: int, y: int) -> None:
        self.db.set_kv("overlay_pos", {"x": int(x), "y": int(y)})

    # ---------------------------------------------------------------- drag: the window follows the cursor
    async def drag_start(self, ox: float, oy: float) -> dict:
        win = await self.window()
        if not win:
            raise ValueError("Окно компаньона не найдено")
        self.drag = {"addr": win["address"], "ox": float(ox), "oy": float(oy), "w": win["size"][0], "h": win["size"][1],
                     "alive": time.monotonic(), "last": tuple(win["at"])}
        if not self._follow_task or self._follow_task.done():
            self._follow_task = asyncio.create_task(self._follow())
        return {"ok": True}

    def drag_move(self) -> dict:
        if self.drag:
            self.drag["alive"] = time.monotonic()      # the page still holds the pointer
        return {"ok": bool(self.drag)}

    async def drag_end(self) -> dict:
        d, self.drag = self.drag, None
        if d:
            self._save(*d["last"])
        return {"ok": True}

    async def _follow(self) -> None:
        area = None
        while self.drag and time.monotonic() - self.drag["alive"] < 0.8:     # a lost pointer-up ends it by itself
            d = self.drag
            cp = await hypr_json("cursorpos")
            if area is None or not (area["x"] <= cp["x"] < area["x"] + area["w"]):
                area = await self.area_at(cp["x"], cp["y"])
            x, y = clamp(cp["x"] - d["ox"], cp["y"] - d["oy"], d["w"], d["h"], area)
            if (x, y) != d["last"]:
                await hypr(f"dispatch movewindowpixel exact {x} {y},address:{d['addr']}")
                d["last"] = (x, y)
            await asyncio.sleep(1 / 60)
        if self.drag:                                     # timed out without pointer-up: keep where it is
            self._save(*self.drag["last"])
            self.drag = None

    # ---------------------------------------------------------------- expand / collapse / place
    async def resize(self, w: int, h: int) -> dict:
        win = await self.window()
        if not win:
            raise ValueError("Окно компаньона не найдено")
        w, h = max(48, min(int(w), 900)), max(48, min(int(h), 1000))
        rect = {"x": win["at"][0], "y": win["at"][1], "w": win["size"][0], "h": win["size"][1]}
        area = await self.area_at(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        x, y = grow(rect, w, h, area)
        addr = win["address"]
        await hypr_batch([f"dispatch resizewindowpixel exact {w} {h},address:{addr}",
                          f"dispatch movewindowpixel exact {x} {y},address:{addr}",
                          f"dispatch setprop address:{addr} rounding {w // 2 if w <= ORB + 8 else 18}"])
        if w <= ORB + 8:
            self._save(x, y)                              # remember where the orb sits
        return {"x": x, "y": y, "w": w, "h": h}

    async def close(self) -> dict:
        """Take the companion off the screen (Super+Shift+J or «jarvis overlay» brings it back)."""
        win = await self.window()
        if win:
            await hypr(f"dispatch closewindow address:{win['address']}")
        return {"closed": bool(win)}

    async def place_default(self) -> dict:
        win = await self.window()
        if not win:
            raise ValueError("Окно компаньона не найдено")
        cp = await hypr_json("cursorpos")
        area = await self.area_at(cp["x"], cp["y"])
        x, y = default_spot(area, win["size"][0])
        await hypr(f"dispatch movewindowpixel exact {x} {y},address:{win['address']}")
        self._save(x, y)
        return {"x": x, "y": y}
