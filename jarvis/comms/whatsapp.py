"""WhatsApp through WhatsApp Web in JARVIS's own browser profile.

There is no official API for personal WhatsApp accounts. JARVIS opens WhatsApp Web in a separate Chromium profile
(~/.local/share/jarvis/whatsapp, mode 0700); the user links it by scanning the QR code with their phone, exactly
like any WhatsApp Web login. JARVIS never reads cookies from other browsers and never exports the session.
WhatsApp's rules discourage automation: this is meant for a person's own occasional messages, not bulk sending."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import time
from pathlib import Path

from .base import CommsError, Contact, Provider, Receipt, Status
from .contacts import AddressBook, is_self, match_score

URL = "https://web.whatsapp.com/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
REQUIREMENT = ("Нужно связать WhatsApp: нажмите «Подключить», затем на телефоне WhatsApp → Настройки → "
               "Связанные устройства → Привязка устройства и отсканируйте QR-код в окне JARVIS.")
# Several candidates per element: WhatsApp Web changes its markup; the first one that matches wins.
SEL_READY = ["#pane-side", "[data-testid='chat-list']", "div[aria-label='Список чатов']", "div[aria-label='Chat list']"]
SEL_QR = ["canvas[aria-label]", "div[data-ref] canvas", "canvas"]
SEL_SEARCH = ["#side div[contenteditable='true']", "div[aria-label='Поиск'][contenteditable='true']",
              "div[aria-label='Search'][contenteditable='true']", "#side input[type='text']", "#side [role='textbox']"]
SEL_COMPOSER = ["footer div[contenteditable='true']", "#main footer [role='textbox']"]
SEL_HEADER_TITLE = ["#main header span[dir='auto'][title]", "#main header span[dir='auto']"]
SEL_INVALID_PHONE = ["div[data-animate-modal-popup='true']", "[role='dialog']"]


def chromium() -> str | None:
    for n in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        p = shutil.which(n)
        if p:
            return p
    return None


class WhatsAppProvider(Provider):
    id = "whatsapp"
    title = "WhatsApp"
    caps = {"can_send_text": True, "can_schedule": True, "can_search_contacts": True, "can_open_app": True,
            "can_send_file": False, "can_send_image": False, "can_reply": False}

    def __init__(self, db, profile: Path, idle_close_s: float = 600):
        self.db, self.profile, self.idle_close_s = db, profile, idle_close_s
        self.book = AddressBook(db)
        self.pw = self.ctx = self.page = None
        self.state, self.detail = ("linked" if self._linked_before() else "not_connected"), ""
        self.qr_png: str | None = None
        self.qr_at = 0.0
        self._lock = asyncio.Lock()
        self._used = 0.0
        self._idle_task: asyncio.Task | None = None
        self._login_task: asyncio.Task | None = None

    def _linked_before(self) -> bool:
        return bool(self.db.get_kv("whatsapp_linked"))

    def status(self) -> Status:
        if not chromium():
            return Status("error", "Не найден Chromium для WhatsApp Web", requirement="Установите chromium")
        if self.state == "connected" or (self.state == "linked" and self._linked_before()):
            return Status("connected", "WhatsApp Web связан с этим компьютером (отдельный профиль JARVIS)",
                          self.db.get_kv("whatsapp_account") or "WhatsApp",
                          extra={"browser_open": self.page is not None})
        if self.state == "qr":
            return Status("connecting", "Отсканируйте QR-код телефоном", extra={"qr": self.qr_png, "qr_at": self.qr_at})
        if self.state == "starting":
            return Status("connecting", "Открываю WhatsApp Web…")
        if self.state == "error":
            return Status("error", self.detail, requirement=REQUIREMENT)
        return Status("not_connected", "WhatsApp не подключён", requirement=REQUIREMENT)

    # ---------------------------------------------------------------- browser lifecycle
    async def _open(self) -> None:
        if self.page and not self.page.is_closed():
            return
        exe = chromium()
        if not exe:
            raise CommsError("Не найден Chromium для WhatsApp Web", "not_connected")
        from playwright.async_api import async_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        try:
            self.pw = self.pw or await async_playwright().start()
            try:
                self.ctx = await self._launch(exe)
            except Exception as e:   # noqa: BLE001
                if "ProcessSingleton" not in str(e) and "already in use" not in str(e):
                    raise
                # A browser left over from a previous JARVIS run still holds OUR profile: stop only that one.
                await self._kill_stale()
                self.ctx = await self._launch(exe)
            self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
            await self.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        except CommsError:
            raise
        except Exception as e:   # noqa: BLE001 — nothing was typed yet: safe to retry later
            await self._close()
            raise CommsError("WhatsApp Web не запустился", "network", f"{e.__class__.__name__}: {str(e)[:200]}",
                             retryable=True) from None
        self._touch()

    async def _launch(self, exe: str):
        return await self.pw.chromium.launch_persistent_context(
            str(self.profile), executable_path=exe, headless=True, user_agent=UA, locale="ru-RU",
            viewport={"width": 1280, "height": 900}, args=["--disable-extensions", "--no-first-run"])

    async def _kill_stale(self) -> None:
        """Stop Chromium processes started with JARVIS's own WhatsApp profile (never the user's browser)."""
        import signal
        marker = f"--user-data-dir={self.profile}"
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmd = (pid_dir / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(c.decode("utf-8", "replace") == marker for c in cmd):
                try:
                    os.kill(int(pid_dir.name), signal.SIGTERM)
                except OSError:
                    pass
        await asyncio.sleep(1.5)
        for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.profile / f).unlink()
            except OSError:
                pass

    async def _close(self) -> None:
        for obj, meth in ((self.ctx, "close"), (self.pw, "stop")):
            if obj:
                try:
                    await getattr(obj, meth)()
                except Exception:   # noqa: BLE001
                    pass
        self.pw = self.ctx = self.page = None

    def _touch(self) -> None:
        self._used = time.time()
        if not self._idle_task or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_closer())

    async def _idle_closer(self) -> None:
        while self.page is not None:
            await asyncio.sleep(30)
            if time.time() - self._used > self.idle_close_s and not self._lock.locked() and self.state != "qr":
                await self._close()
                return

    async def stop(self) -> None:
        for t in (self._login_task, self._idle_task):
            if t:
                t.cancel()
        await self._close()

    async def _first(self, sels: list[str], timeout: float = 0.0):
        """First visible element among the candidates (polls up to `timeout` seconds)."""
        end = time.time() + timeout
        while True:
            for s in sels:
                try:
                    el = await self.page.query_selector(s)
                    if el and await el.is_visible():
                        return el
                except Exception:   # noqa: BLE001
                    pass
            if time.time() >= end:
                return None
            await asyncio.sleep(0.4)

    async def _wait_ready(self, timeout: float) -> str:
        """'ready' | 'qr' | 'timeout'."""
        end = time.time() + timeout
        while time.time() < end:
            if await self._first(SEL_READY):
                return "ready"
            if await self._first(SEL_QR):
                return "qr"
            await asyncio.sleep(0.5)
        return "timeout"

    # ---------------------------------------------------------------- linking (user scans the QR code)
    async def begin_link(self) -> dict:
        if self._login_task and not self._login_task.done():
            return {"stage": self.state}
        self.state = "starting"
        self._login_task = asyncio.create_task(self._link_loop())
        return {"stage": "starting"}

    async def _link_loop(self) -> None:
        try:
            async with self._lock:
                await self._open()
                end = time.time() + 180
                while time.time() < end:
                    st = await self._wait_ready(5)
                    if st == "ready":
                        await self._linked()
                        return
                    if st == "qr":
                        el = await self._first(["div[data-ref]"] + SEL_QR)
                        if el:
                            self.qr_png = base64.b64encode(await el.screenshot()).decode()
                            self.qr_at = time.time()
                            self.state = "qr"
                    await asyncio.sleep(2)
                self.state, self.detail, self.qr_png = "error", "QR-код не отсканирован за 3 минуты — попробуйте снова", None
                await self._close()
        except CommsError as e:
            self.state, self.detail = "error", str(e)
        except Exception as e:   # noqa: BLE001
            self.state, self.detail = "error", "WhatsApp Web не открылся: " + e.__class__.__name__
            await self._close()

    async def _linked(self) -> None:
        self.state, self.qr_png = "connected", None
        self.db.set_kv("whatsapp_linked", True)
        self._touch()

    async def logout(self) -> None:
        await self.stop()
        shutil.rmtree(self.profile, ignore_errors=True)       # the link can also be removed on the phone
        self.db.set_kv("whatsapp_linked", False)
        self.state, self.detail, self.qr_png = "not_connected", "", None

    async def _ensure(self) -> None:
        if not self._linked_before():
            raise CommsError("WhatsApp не подключён. " + REQUIREMENT, "not_connected", retryable=True)
        await self._open()
        st = await self._wait_ready(45)
        if st == "qr":
            self.db.set_kv("whatsapp_linked", False)
            self.state = "not_connected"
            await self._close()
            raise CommsError("WhatsApp отвязал этот компьютер — подключите заново (QR-код)", "auth", retryable=False)
        if st != "ready":
            raise CommsError("WhatsApp Web не загрузился", "network", retryable=True)
        self.state = "connected"
        self._touch()

    # ---------------------------------------------------------------- contacts
    async def search(self, name: str) -> list[Contact]:
        if is_self(name):
            own = re.sub(r"\D", "", self.db.get_kv("whatsapp_phone") or "")
            if own:
                return [Contact("whatsapp", "phone:" + own, "Вы (заметки)", "+" + own, "ваш номер")]
        digits = re.sub(r"\D", "", name)
        if len(digits) >= 10:
            return [Contact("whatsapp", "phone:" + digits, "+" + digits, "+" + digits, "номер из команды")]
        out = self.book.find(name, "whatsapp")
        async with self._lock:
            await self._ensure()
            box = await self._first(SEL_SEARCH, 10)
            if not box:
                raise CommsError("Не нашёл поиск в WhatsApp Web", "unknown", "search box selector")
            await box.click()
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(name)
            await asyncio.sleep(1.5)
            titles = await self.page.eval_on_selector_all(
                "#pane-side span[title], [aria-label] [role='listitem'] span[title]",
                "els => [...new Set(els.map(e => e.getAttribute('title')).filter(Boolean))].slice(0, 30)")
            await self.page.keyboard.press("Escape")
        for t in titles:
            sc = match_score(name, t)
            if sc and not re.fullmatch(r"[\d\s:.,]+", t):
                out.append(Contact("whatsapp", "chat:" + t, t, "чат WhatsApp", "WhatsApp", sc))
        return out

    # ---------------------------------------------------------------- send / verify
    async def _open_chat(self, contact: Contact) -> None:
        kind, _, key = contact.id.partition(":")
        if kind == "phone":
            await self.page.goto(f"{URL}send?phone={key}", wait_until="domcontentloaded", timeout=45000)
            if await self._wait_ready(45) != "ready":
                raise CommsError("WhatsApp Web не загрузился", "network", retryable=True)
            comp = await self._first(SEL_COMPOSER, 25)
            if not comp:
                if await self._first(SEL_INVALID_PHONE):
                    raise CommsError(f"Номера +{key} нет в WhatsApp", "not_found")
                raise CommsError("Не открылся чат WhatsApp", "unknown", "composer not found", retryable=True)
            return
        box = await self._first(SEL_SEARCH, 10)
        if not box:
            raise CommsError("Не нашёл поиск в WhatsApp Web", "unknown", retryable=True)
        await box.click()
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        await self.page.keyboard.insert_text(key)
        await asyncio.sleep(1.5)
        target = None
        for el in await self.page.query_selector_all("#pane-side span[title]"):
            if (await el.get_attribute("title")) == key:
                target = el
                break
        if not target:
            raise CommsError(f"Чат «{key}» не найден в WhatsApp", "not_found")
        await target.click()
        head = await self._first(SEL_HEADER_TITLE, 10)
        title = (await head.get_attribute("title") or await head.inner_text()) if head else ""
        if title.strip() != key:
            # Never type into a chat we are not sure about.
            raise CommsError("Открылся не тот чат WhatsApp — отправка остановлена", "rejected", f"header={title!r}",
                             retryable=True)
        if not await self._first(SEL_COMPOSER, 10):
            raise CommsError("В этот чат WhatsApp нельзя написать", "rejected")

    async def send(self, contact: Contact, body: str, subject: str | None, op_id: str) -> Receipt:
        async with self._lock:
            await self._ensure()
            await self._open_chat(contact)             # failures up to here: nothing was typed → retryable
            before = (await self._last_out(body) or {}).get("count", 0)
            comp = await self._first(SEL_COMPOSER, 5)
            await comp.click()
            lines = body.split("\n")
            for i, line in enumerate(lines):
                if i:
                    await self.page.keyboard.press("Shift+Enter")
                if line:
                    await self.page.keyboard.insert_text(line)
            await self.page.keyboard.press("Enter")
            self._touch()
            # Wait for our bubble to appear with a "sent" tick (not just the clock icon).
            for _ in range(40):
                await asyncio.sleep(0.5)
                st = await self._last_out(body)
                if st and st["count"] > before and st["status"] in ("sent", "delivered", "read"):
                    return Receipt(f"wa:{int(time.time())}:{st['count']}", "WhatsApp отметил сообщение как отправленное")
            st = await self._last_out(body)
            await self._debug_snapshot(st)
            if st and st["count"] > before:
                raise CommsError("Сообщение в WhatsApp ещё не отправлено (висит с часиками) — проверьте связь телефона",
                                 "unknown", str(st))
            raise CommsError("WhatsApp не показал отправленное сообщение — статус неизвестен", "unknown", str(st))

    async def _debug_snapshot(self, st) -> None:
        """Local-only evidence when the outcome is unclear: the markup around OUR message only (no screenshot of the
        chat list), in the private data dir, overwritten each time."""
        try:
            path = self.profile.parent / "whatsapp-last-unknown.json"
            path.write_text(json.dumps(st, ensure_ascii=False)[:20000], "utf-8")
            os.chmod(path, 0o600)
        except Exception:   # noqa: BLE001
            pass

    async def _last_out(self, body: str) -> dict | None:
        """Find the message we typed by its text in the open chat and read its status ticks.
        Measured 2026-10-21: the old `.message-out` class no longer exists in WhatsApp Web, so markup classes are not
        relied on — only the text we sent and the status icon/label next to it."""
        return await self.page.evaluate("""(want) => {
          const norm = (x) => (x || '').replace(/\\s+/g, ' ').trim();
          const w = norm(want);
          const root = document.querySelector('#main') || document.body;
          let hits = [...root.querySelectorAll('span, div')]
            .filter((e) => !e.closest('footer') && !e.isContentEditable && norm(e.innerText) === w);   // not the input box
          hits = hits.filter((e) => !hits.some((o) => o !== e && e.contains(o)));      // innermost only
          if (!hits.length) return { count: 0, status: 'none' };
          const RX = /check|time|clock|pending|status|отправлен|доставлен|прочитан|ожидани|sent|delivered|read/i;
          let el = hits[hits.length - 1], marks = [], bubble = el;
          for (let i = 0; i < 12 && bubble.parentElement && bubble !== root; i++) {
            bubble = bubble.parentElement;
            marks = [...bubble.querySelectorAll('[data-icon], [aria-label]')]
              .map((m) => ((m.getAttribute('data-icon') || '') + ' ' + (m.getAttribute('aria-label') || '')).trim())
              .filter((s) => RX.test(s));
            if (marks.length) break;
          }
          const s = marks.join(' | ').toLowerCase();
          const status = /ack|read|прочитан/.test(s) ? 'read' : /dblcheck|delivered|доставлен/.test(s) ? 'delivered'
                       : /check|sent|отправлен/.test(s) ? 'sent' : /time|clock|pending|ожидани/.test(s) ? 'pending' : 'unknown';
          return { count: hits.length, status, marks: marks.slice(0, 6), matches: true,
                   html: status === 'unknown' ? bubble.outerHTML.slice(0, 6000) : undefined };
        }""", body)

    async def verify(self, contact: Contact, body: str, receipt: Receipt) -> tuple[str, str]:
        try:
            async with self._lock:
                st = await self._last_out(body) if self.page else None
        except Exception:   # noqa: BLE001
            st = None
        if st and st.get("matches") and st["status"] in ("sent", "delivered", "read"):
            word = {"sent": "отправлено (одна галочка)", "delivered": "доставлено", "read": "прочитано"}[st["status"]]
            return "verified", f"WhatsApp: последнее сообщение в чате «{contact.name}» — {word}"
        return "unverified", "Не удалось перечитать чат WhatsApp после отправки"
