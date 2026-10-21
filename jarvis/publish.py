"""Publishing a finished site on the internet: GitHub Pages through the user's own GitHub account (GitHub CLI).

Connecting the account is user-driven from the window: GitHub's device login shows a one-time code that the user
confirms in their own browser — JARVIS never sees a password or token (gh keeps it in its own store, which is closed
to every JARVIS tool). Publishing makes the folder public, so it is a high-risk, external action with the user's
confirmation, a check for secret files, and a real check that the page opens at its address."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path

from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification, is_secret_path, run_cmd

CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
URL_RE = re.compile(r"https://github\.com/login/device\S*")
SECRETISH = re.compile(r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.(pem|key|kdbx|p12|pfx)|credentials(\.json)?)$", re.I)
_GH_CRED = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]   # this command only


def gh_available() -> bool:
    return bool(shutil.which("gh") and shutil.which("git"))


class GitHub:
    """GitHub account state and the device login started from the window."""

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.code: str | None = None
        self.url: str | None = None
        self.started_at = 0.0
        self.error: str | None = None
        self._user: dict | None = None

    async def user(self, refresh: bool = False) -> dict | None:
        if not gh_available():
            return None
        if self._user and not refresh:
            return self._user
        r = await run_cmd(["gh", "api", "user", "--jq", ".login + \" \" + (.id|tostring)"], timeout=15)
        if r.code != 0 or not r.stdout.strip():
            self._user = None
            return None
        login, uid = r.stdout.strip().split()[:2]
        self._user = {"login": login, "id": uid}
        return self._user

    async def state(self) -> dict:
        if not gh_available():
            return {"available": False, "connected": False}
        if self.proc and self.proc.returncode is None:
            return {"available": True, "connected": False, "stage": "code", "code": self.code, "url": self.url}
        u = await self.user(refresh=True)
        return {"available": True, "connected": bool(u), "login": (u or {}).get("login"), "error": self.error}

    async def begin(self) -> dict:
        """Start GitHub's device login: returns the one-time code the user enters at github.com/login/device."""
        if not gh_available():
            raise ValueError("GitHub CLI (gh) не установлен")
        if self.proc and self.proc.returncode is None:
            return await self.state()
        self.code = self.url = self.error = None
        env = {**os.environ, "BROWSER": "true", "GH_PROMPT_DISABLED": "1"}   # the window opens the page itself
        self.proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--skip-ssh-key",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, start_new_session=True)
        self.started_at = time.time()
        end = time.time() + 20
        while time.time() < end and not self.code:
            line = await asyncio.wait_for(self.proc.stdout.readline(), max(0.1, end - time.time()))
            if not line:
                break
            s = line.decode("utf-8", "replace")
            self.code = self.code or (CODE_RE.search(s).group(1) if CODE_RE.search(s) else None)
            self.url = self.url or (URL_RE.search(s).group(0) if URL_RE.search(s) else None)
        if not self.code:
            await self.cancel()
            raise ValueError("GitHub не выдал код входа — попробуйте ещё раз")
        self.url = self.url or "https://github.com/login/device"
        from .events import spawn
        spawn(self._wait(), "github-login")
        return {"stage": "code", "code": self.code, "url": self.url}

    async def _wait(self) -> None:
        proc = self.proc
        try:
            await asyncio.wait_for(proc.wait(), 900)
        except asyncio.TimeoutError:
            await self.cancel()
            self.error = "Код не подтверждён за 15 минут — начните заново"
            return
        if proc.returncode != 0:
            self.error = "Вход в GitHub не завершён"
        self._user = None

    async def cancel(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, 9)            # gh and anything it started (own session)
            except ProcessLookupError:
                pass
            await self.proc.wait()

    async def logout(self) -> None:
        await self.cancel()
        u = await self.user(refresh=True)
        if u:
            await run_cmd(["gh", "auth", "logout", "--hostname", "github.com", "--user", u["login"]], timeout=20)
        self._user = None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-._")[:60]
    return s or "site"


class SitePublish(Tool):
    def __init__(self, github: GitHub, config):
        self.github, self.config = github, config
        self.spec = ToolSpec(
            id="site_publish", title="Публикация сайта в интернете",
            description=("Publish a finished static website folder (with index.html) on the internet with GitHub "
                         "Pages through the user's GitHub account, so it opens by a public link. Use it when the user "
                         "asks to publish / put online / give a link to a site. name = short latin name for the "
                         "address. Everything in the folder becomes public. The user confirms first."),
            params={"type": "object", "required": ["folder"], "properties": {
                "folder": {"type": "string", "maxLength": 500}, "name": {"type": "string", "maxLength": 60}}},
            risk=Risk.HIGH, side_effects="external", timeout_s=600, model_callable=True, idempotent=False,
            verification="Страница открывается по публичной ссылке (HTTP 200)",
            impact="Содержимое папки станет публичным: репозиторий на вашем GitHub и сайт по ссылке, видимый всем. "
                   "Файлы с ключами и паролями не публикуются — если такие есть, JARVIS остановится.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        from .agency.tools import _in_workspaces
        folder = _in_workspaces(a["folder"], self.config)
        if not (folder / "index.html").is_file():
            raise ToolInputError("В папке нет index.html — это не готовый сайт")
        bad, total = [], 0
        for p in folder.rglob("*"):
            if ".git" in p.parts or "node_modules" in p.parts:
                continue
            if p.is_file():
                total += p.stat().st_size
                rel = p.relative_to(folder).as_posix()
                if SECRETISH.search(rel) or is_secret_path(p):
                    bad.append(rel)
        if bad:
            raise ToolInputError("В папке есть файлы, похожие на ключи или пароли — публиковать нельзя: " +
                                 ", ".join(bad[:5]))
        if total > 200 * 1024 * 1024:
            raise ToolInputError("Папка больше 200 МБ — для GitHub Pages слишком большая")
        if not gh_available():
            raise ToolInputError("Для публикации нужен GitHub CLI (gh)")
        return {"folder": str(folder), "name": _slug(a.get("name") or folder.name)}

    def describe(self, args):
        return (f"Опубликовать сайт из {args['folder'].replace(os.path.expanduser('~'), '~')} на GitHub Pages "
                f"(репозиторий «{args['name']}», виден всем)")

    async def _git(self, folder: str, *a: str, timeout: float = 60):
        return await run_cmd(["git", *_GH_CRED, "-C", folder, *a], timeout=timeout)

    async def run(self, args, ctx):
        u = await self.github.user(refresh=True)
        if not u:
            raise ToolInputError("GitHub не подключён: Настройки → Публикация сайтов → «Подключить GitHub»")
        f, name, login = args["folder"], args["name"], u["login"]
        ctx.progress("Готовлю файлы", None)
        if not Path(f, ".git").exists():
            await run_cmd(["git", "init", "-q", f], timeout=20)
        gi = Path(f, ".gitignore")
        keep = gi.read_text() if gi.exists() else ""
        need = [x for x in (".env", ".env.*", "*.pem", "*.key", "node_modules/") if x not in keep.split()]
        if need:
            gi.write_text(keep + ("" if keep.endswith("\n") or not keep else "\n") + "\n".join(need) + "\n")
        await self._git(f, "config", "user.name", login)
        await self._git(f, "config", "user.email", f"{u['id']}+{login}@users.noreply.github.com")
        await self._git(f, "add", "-A")
        await self._git(f, "commit", "-q", "-m", "Сайт опубликован JARVIS")
        await self._git(f, "branch", "-M", "main")
        ctx.progress("Отправляю на GitHub", None)
        has_remote = (await self._git(f, "remote", "get-url", "origin")).code == 0
        if not has_remote:
            r = await run_cmd(["gh", "repo", "create", name, "--public", "--source", f, "--remote", "origin"],
                              timeout=60)
            if r.code != 0:
                raise ToolInputError("GitHub не создал репозиторий: " + (r.stderr or r.stdout).strip()[:200])
        r = await self._git(f, "push", "-u", "origin", "main", timeout=180)
        if r.code != 0:
            raise ToolInputError("Не удалось отправить файлы на GitHub: " + (r.stderr or r.stdout).strip()[-200:])
        remote = (await self._git(f, "remote", "get-url", "origin")).stdout.strip()
        m = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", remote)
        owner, repo = (m.group(1), m.group(2)) if m else (login, name)
        ctx.progress("Включаю GitHub Pages", None)
        await run_cmd(["gh", "api", "-X", "POST", f"repos/{owner}/{repo}/pages",
                       "-f", "source[branch]=main", "-f", "source[path]=/"], timeout=30)   # 409 = already on
        url = f"https://{owner}.github.io/{repo}/"
        return ToolResult({"url": url, "repo": f"https://github.com/{owner}/{repo}", "folder": f},
                          f"Сайт опубликован: {url}")

    async def verify(self, args, result, ctx):
        url = result.data["url"]
        ctx.progress("Жду, пока GitHub соберёт сайт", None)
        for _ in range(30):                         # GitHub Pages usually needs 30–90 s for the first build
            try:
                code = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).status)
                if code == 200:
                    return Verification("verified", f"Сайт открывается: {url}")
            except Exception:   # noqa: BLE001
                pass
            await asyncio.sleep(5)
        return Verification("unverified", f"GitHub ещё собирает сайт — ссылка заработает через пару минут: {url}")
