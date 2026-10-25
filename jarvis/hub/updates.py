"""Install, update, roll back and quarantine OpenClaw skills from ClawHub, with JARVIS's own checks on top of
OpenClaw's (ported from morph-rq/jarvis-os).

OpenClaw does the installing (`skills.install` / `skills.update`, source "clawhub"): it checks ClawHub's security
verdict, blocks releases ClawHub marks malicious, and swaps the folder with a backup. What JARVIS adds:

- Install only what the person approved, at the version they saw. A release whose scan is not clean is refused.
- After installing, the SKILL.md on disk must hash to the card the person reviewed (OpenClaw's gateway path checks no
  expected hash); a different or unreadable one is turned off.
- Updates are compared before they are applied. A new major version, new requirements (binaries, keys, config), a scan
  that is not clean, or a component not on the trusted list waits for approval. Only trusted, clean, same-requirements
  updates may run unattended, and only when nothing else is running.
- Before an update the skill's folder is copied aside; after it a smoke test checks that OpenClaw still lists the skill,
  that its SKILL.md has a name and description and the same name, and that the security verdict passes. On failure the
  copy is restored (rollback), the release is quarantined (never retried automatically), and the person is told once.
- Every step lands in update_history, which the panel's «Обновления» shows.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from .hermes import parse_frontmatter


class UpdateError(Exception):
    pass


def vtuple(v: str) -> tuple:
    parts = re.findall(r"\d+|[a-z]+", str(v or "").lower())
    return tuple(int(p) if p.isdigit() else -1 for p in parts) or (0,)


def newer(a: str, b: str) -> bool:
    return vtuple(a) > vtuple(b)


def requirements(skill_md: str) -> set[str]:
    """What a SKILL.md asks for: binaries, environment keys, config paths, OS (metadata.openclaw.requires)."""
    m = re.match(r"^---\s*\n(.*?)\n---", skill_md or "", re.S)
    head = m.group(1) if m else ""
    out: set[str] = set()
    for k, prefix in (("bins", "bin"), ("anyBins", "bin"), ("env", "env"), ("config", "config"), ("os", "os")):
        for block in re.findall(rf'"?{k}"?\s*:\s*\[([^\]]*)\]', head):
            for item in re.findall(r'"([^"]+)"|\'([^\']+)\'|([\w./:-]+)', block):
                out.add(f"{prefix}:{next(x for x in item if x)}")
    for env in re.findall(r'"?primaryEnv"?\s*:\s*"([^"]+)"', head):
        out.add(f"env:{env}")
    tools = re.search(r"^allowed-tools:\s*(.+)$", head, re.M)
    if tools:
        out |= {f"tool:{t.strip()}" for t in re.split(r"[,\s]+", tools.group(1)) if t.strip()}
    return out


def card_text(detail: dict) -> str:
    """The release's SKILL.md from skills.detail ({card: {status, content}})."""
    card = detail.get("card")
    if isinstance(card, str):
        return card
    if isinstance(card, dict) and card.get("status") == "available":
        return str(card.get("content") or "")
    return ""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Lifecycle:
    """Needs OpenClaw connected for anything that changes state."""

    def __init__(self, db, openclaw, hub, *, backups: Path, policy: dict, busy=lambda: False, notify=None) -> None:
        self.db, self.oc, self.hub = db, openclaw, hub
        self.backups = backups
        self.policy = policy              # {"trusted": [...], "auto_update_skills": "off"|"trusted"}
        self.busy = busy                  # () -> True while the person's tasks run: unattended updates wait
        self.notify = notify              # (title, body) -> None

    # ------------------------------------------------------------ state
    async def installed(self) -> dict[str, dict]:
        """ClawHub-installed skills by slug: {version, dir, key, eligible, path, owner, name}."""
        raw = await self.oc.c.request("skills.status", {}, timeout=20) or {}
        out = {}
        for s in raw.get("skills") or []:
            link = s.get("clawhub") or {}
            if link.get("status") == "linked":
                out[link.get("slug") or s.get("name")] = {
                    "version": str(link.get("installedVersion") or ""), "dir": s.get("baseDir", ""),
                    "key": s.get("skillKey") or s.get("name"), "eligible": bool(s.get("eligible")),
                    "disabled": bool(s.get("disabled")), "path": s.get("filePath", ""), "owner": link.get("ownerHandle", ""),
                    "registry": link.get("registry", ""), "name": s.get("name", "")}
        self.hub.installed = {k: v["version"] for k, v in out.items()}
        return out

    async def text(self, info: dict) -> str | None:
        """An installed skill's SKILL.md: from disk, or through the gateway. None when neither works."""
        try:
            if info.get("path"):
                return Path(info["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        try:
            card = await self.oc.c.request("skills.skillCard", {"skillKey": info["key"]}, timeout=15) or {}
            return str(card.get("content") or "")
        except Exception:   # noqa: BLE001
            return None

    def history(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM update_history ORDER BY id DESC LIMIT ?", (limit,))]

    def available(self) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM update_available ORDER BY needs_approval DESC, component"):
            d = dict(r)
            d["new_requirements"] = json.loads(d["new_requirements"] or "[]")
            d["reasons"] = json.loads(d["reasons"] or "[]")
            out.append(d)
        return out

    def quarantined(self, component: str, version: str) -> bool:
        return self.db.one("SELECT 1 FROM update_quarantine WHERE component = ? AND version = ?",
                           (component, version)) is not None

    def _log(self, component: str, kind: str, old: str, new: str, status: str, detail: str = "", auto: bool = False):
        self.db.execute("INSERT INTO update_history(ts, component, kind, from_version, to_version, status, auto, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (time.time(), component, kind, old, new, status, int(auto),
                                                            detail[:1500]))

    # ------------------------------------------------------------ install
    async def review(self, ref: str, version: str = "") -> dict:
        """Everything the approval card shows for installing a skill, from ClawHub through OpenClaw."""
        d = await self.oc.skill_detail(ref, version)
        skill = d.get("skill") or {}
        latest = d.get("latestVersion") or {}
        release = d.get("selectedRelease") or {}
        ver = str(version or release.get("version") or latest.get("version") or "")
        sec = d.get("security") or {}
        card = card_text(d)
        return {"ref": d.get("installRef") or ref, "slug": skill.get("slug", ref), "title": skill.get("displayName", ref),
                "summary": str(skill.get("summary", ""))[:400], "owner": (d.get("owner") or {}).get("handle", ""),
                "version": ver, "changelog": str(latest.get("changelog", ""))[:600],
                "scan": sec.get("scanStatus") or sec.get("status") or "unknown",
                "requirements": sorted(requirements(card)), "card_sha": sha(card) if card else "",
                "warnings": [str(w)[:200] for w in d.get("warnings") or []], "registry": d.get("registry", "")}

    async def install(self, ref: str, review: dict) -> dict:
        if review.get("scan") != "clean":
            self.hub.event("blocked", review.get("slug", ref), review.get("version", ""), f"scan {review.get('scan')}")
            raise UpdateError(f"Проверка безопасности ClawHub для этой версии: «{review.get('scan')}», а не «clean» — "
                              "не устанавливаю.")
        if self.quarantined(review["slug"], review["version"]):
            raise UpdateError(f"{review['slug']} {review['version']} в карантине после неудачной проверки.")
        result = await self.oc.skill_install(review["ref"], review["version"])
        slug, version = result.get("slug") or review["slug"], result.get("version") or review["version"]
        state = await self._wait_listed(slug, version)
        if state is None:
            raise UpdateError("OpenClaw сообщил об установке, но навыка нет в его списке.")
        text = await self.text(state)
        if review.get("card_sha") and text is None:
            await self.oc.skill_enable(state["key"], False)
            self._log(slug, "skill", "", version, "unverified", "installed SKILL.md unreadable")
            raise UpdateError("Установлен, но прочитать установленный SKILL.md и сравнить с одобренным не удалось — "
                              "навык выключен.")
        if review.get("card_sha") and sha(text) != review["card_sha"]:
            await self.oc.skill_enable(state["key"], False)
            self._quarantine(slug, version, "installed SKILL.md differs from the reviewed card")
            raise UpdateError("Установленный SKILL.md не тот, что вы одобрили: навык выключен и помещён в карантин.")
        self.hub.event("installed", slug, version)
        self._log(slug, "skill", "", version, "installed", result.get("warning") or "")
        self.db.execute("DELETE FROM update_available WHERE component = ?", (slug,))
        return {"ok": True, "slug": slug, "version": version, "key": state["key"], "eligible": state["eligible"],
                "warning": result.get("warning") or "", "dir": state["dir"]}

    async def _wait_listed(self, slug: str, version: str, timeout: float = 10) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inst = await self.installed()
            if slug in inst and (not version or inst[slug]["version"] == version):
                return inst[slug]
            await asyncio.sleep(0.5)
        return None

    async def set_enabled(self, slug: str, enabled: bool) -> dict:
        inst = await self.installed()
        if slug not in inst:
            raise UpdateError(f"{slug} не установлен из ClawHub.")
        await self.oc.skill_enable(inst[slug]["key"], enabled)
        self.hub.event("enabled" if enabled else "disabled", slug, inst[slug]["version"])
        return {"ok": True, "slug": slug, "enabled": enabled}

    async def uninstall(self, slug: str) -> dict:
        """Disable in OpenClaw, then move the folder into JARVIS's backups (restorable). OpenClaw has no uninstall."""
        inst = await self.installed()
        if slug not in inst:
            raise UpdateError(f"{slug} не установлен из ClawHub.")
        info = inst[slug]
        await self.oc.skill_enable(info["key"], False)
        dest = self.backups / slug / f"{info['version']}-removed-{int(time.time())}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(info["dir"], dest)
        self.hub.event("uninstalled", slug, info["version"])
        self._log(slug, "skill", info["version"], "", "uninstalled", f"kept in {dest}")
        return {"ok": True, "slug": slug, "kept": str(dest)}

    # ------------------------------------------------------------ updates
    async def check(self) -> list[dict]:
        """Compare installed ClawHub skills with the registry; record what is available and why it would need
        approval. Changes nothing."""
        inst = await self.installed()
        trusted = set(self.policy.get("trusted") or [])
        out = []
        for slug, info in inst.items():
            try:
                d = await self.oc.skill_detail(f"@{info['owner']}/{slug}" if info.get("owner") else slug)
            except Exception:   # noqa: BLE001
                continue
            latest = str((d.get("latestVersion") or {}).get("version") or "")
            if not latest or not newer(latest, info["version"]):
                self.db.execute("DELETE FROM update_available WHERE component = ?", (slug,))
                continue
            sec = (d.get("security") or {}).get("scanStatus") or (d.get("security") or {}).get("status") or "unknown"
            card = card_text(d)
            current = await self.text(info)
            added = (["неизвестно: текст новой версии недоступен"] if not card else
                     ["неизвестно: установленный навык не читается"] if current is None else
                     sorted(requirements(card) - requirements(current)))
            reasons = []
            if added:
                reasons.append("просит больше: " + ", ".join(added))
            if sec != "clean":
                reasons.append(f"проверка: {sec}")
            if vtuple(latest)[:1] != vtuple(info["version"])[:1]:
                reasons.append("новая основная версия")
            if slug not in trusted:
                reasons.append("нет в списке доверенных")
            if self.quarantined(slug, latest):
                reasons.append("в карантине после неудачной проверки")
            row = {"component": slug, "kind": "skill", "current": info["version"], "latest": latest,
                   "changelog": str((d.get("latestVersion") or {}).get("changelog", ""))[:1000], "verdict": sec,
                   "new_requirements": added, "needs_approval": bool(reasons), "reasons": reasons}
            self.db.execute(
                "INSERT OR REPLACE INTO update_available(component, kind, current, latest, changelog, verdict, "
                "new_requirements, needs_approval, reasons, checked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, "skill", info["version"], latest, row["changelog"], sec, json.dumps(added, ensure_ascii=False),
                 int(bool(reasons)), json.dumps(reasons, ensure_ascii=False), time.time()))
            out.append(row)
        return out

    async def apply(self, slug: str, *, approved: bool, auto: bool = False) -> dict:
        """Stage, update, smoke-test; roll back and quarantine on failure."""
        row = {r["component"]: r for r in self.available()}.get(slug)
        if row is None:
            raise UpdateError(f"Для {slug} обновление не найдено — сначала проверьте обновления.")
        if row["needs_approval"] and not approved:
            raise UpdateError(f"{slug} {row['latest']} нужно ваше подтверждение: {'; '.join(row['reasons'])}")
        if self.quarantined(slug, row["latest"]):
            raise UpdateError(f"{slug} {row['latest']} в карантине.")
        if auto and self.busy():
            return {"ok": False, "deferred": True, "message": "JARVIS занят — обновление подождёт."}
        info = (await self.installed()).get(slug)
        if info is None:
            raise UpdateError(f"{slug} больше не установлен.")
        if not Path(info["dir"]).is_dir():
            raise UpdateError(f"Папки {slug} нет на этом компьютере — копию для отката сохранить нельзя.")
        backup = self.backups / slug / f"{info['version']}-{int(time.time() * 1000)}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(info["dir"], backup, symlinks=True)
        old = info["version"]
        try:
            ref = f"@{info['owner']}/{slug}" if info.get("owner") else slug
            result = await self.oc.skill_update(ref)
        except Exception as exc:   # noqa: BLE001
            msg = str(exc)[:400]
            status = "rejected" if "blocked" in msg.lower() else "failed"
            if status == "rejected":
                self._quarantine(slug, row["latest"], msg)
            self._log(slug, "skill", old, row["latest"], status, msg, auto)
            self.hub.event(status, slug, row["latest"], msg)
            return {"ok": False, "status": status, "message": f"Обновление не применено (OpenClaw: {msg}). Осталась {old}."}
        problems = await self.smoke(slug, row["latest"])
        if problems:
            self._restore(info["dir"], backup)
            self._quarantine(slug, row["latest"], "; ".join(problems))
            self._log(slug, "skill", old, row["latest"], "rolled_back", "; ".join(problems), auto)
            self.hub.event("rolled_back", slug, row["latest"], "; ".join(problems))
            await self._wait_listed(slug, old, timeout=5)
            if self.notify:
                self.notify(f"{slug} {row['latest']} откачен", "; ".join(problems)[:200])
            return {"ok": False, "status": "rolled_back", "problems": problems,
                    "message": f"{slug} {row['latest']} не прошёл проверку и откачен к {old}."}
        self.db.execute("DELETE FROM update_available WHERE component = ?", (slug,))
        self._log(slug, "skill", old, row["latest"], "applied", json.dumps((result or {}).get("config", {}))[:500], auto)
        self.hub.event("updated", slug, row["latest"])
        return {"ok": True, "status": "applied", "from": old, "to": row["latest"], "backup": str(backup)}

    async def smoke(self, slug: str, version: str) -> list[str]:
        """Checks after an update; [] = passed."""
        state = await self._wait_listed(slug, version, timeout=8)
        if state is None:
            got = (await self.installed()).get(slug, {}).get("version", "нет")
            return [f"OpenClaw показывает {slug} версии {got}, ожидалась {version}"]
        problems = []
        text = await self.text(state)
        if text is None:
            return ["SKILL.md не читается"]
        fm = parse_frontmatter(text)
        if not fm.get("name") or not fm.get("description"):
            problems.append("в SKILL.md нет name/description")
        elif fm["name"] != state["name"]:
            problems.append(f"навык переименовал себя ({state['name']} → {fm['name']})")
        try:
            v = await self.oc.c.request("skills.securityVerdicts", {}, timeout=30) or {}
            item = next((i for i in v.get("items") or [] if i.get("slug") == slug), None)
            if item is not None and not item.get("ok"):
                problems.append(f"вердикт безопасности: {item.get('decision')}")
        except Exception as exc:   # noqa: BLE001
            problems.append(f"вердикт безопасности недоступен: {str(exc)[:120]}")
        return problems

    def _restore(self, target: str, backup: Path) -> None:
        tmp = Path(target).with_name(Path(target).name + ".failed")
        if tmp.exists():
            shutil.rmtree(tmp)
        if Path(target).exists():
            Path(target).rename(tmp)
        shutil.copytree(backup, target, symlinks=True)
        shutil.rmtree(tmp, ignore_errors=True)
        lock = Path(target).parent.parent / ".clawhub" / "lock.json"
        origin = Path(target) / ".clawhub" / "origin.json"
        if lock.exists() and origin.exists():
            # OpenClaw's lock must agree with the restored origin record, or it shows the link as invalid.
            try:
                data = json.loads(lock.read_text())
                o = json.loads(origin.read_text())
                entry = data.setdefault("skills", {}).setdefault(o.get("slug", Path(target).name), {})
                entry.update(version=o.get("installedVersion"), registry=o.get("registry"),
                             installedAt=o.get("installedAt"), artifact=o.get("artifact"),
                             skillFile=o.get("skillFile"), fileTreeSha256=o.get("fileTreeSha256"))
                if o.get("ownerHandle"):
                    entry["ownerHandle"] = o["ownerHandle"]
                tmp_lock = lock.with_suffix(".jarvis-tmp")
                tmp_lock.write_text(json.dumps(data, indent=2))
                tmp_lock.replace(lock)
            except (OSError, ValueError):
                pass

    def _quarantine(self, component: str, version: str, reason: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO update_quarantine(component, version, reason, ts) VALUES (?, ?, ?, ?)",
                        (component, version, reason[:500], time.time()))
        for row in self.db.all("SELECT reasons FROM update_available WHERE component = ? AND latest = ?",
                               (component, version)):
            reasons = json.loads(row["reasons"] or "[]") + ["в карантине после неудачной проверки"]
            self.db.execute("UPDATE update_available SET needs_approval = 1, reasons = ? WHERE component = ?",
                            (json.dumps(sorted(set(reasons)), ensure_ascii=False), component))
        self.hub.event("quarantined", component, version, reason)

    def release(self, component: str, version: str = "") -> dict:
        """The person lets a quarantined release be tried again (it still goes through the normal approval)."""
        if version:
            n = self.db.execute("DELETE FROM update_quarantine WHERE component = ? AND version = ?",
                                (component, version)).rowcount
        else:
            n = self.db.execute("DELETE FROM update_quarantine WHERE component = ?", (component,)).rowcount
        if n:
            self.hub.event("released", component, version, "quarantine lifted by the person")
            self._log(component, "skill", "", version, "released", "quarantine lifted by the person")
        return {"ok": bool(n), "message": f"{component} можно пробовать снова." if n else f"{component} не в карантине."}

    async def rollback(self, slug: str) -> dict:
        """Back to the newest saved copy of an installed skill (the version before the last update)."""
        info = (await self.installed()).get(slug)
        if info is None:
            raise UpdateError(f"{slug} не установлен.")
        copies = sorted((p for p in (self.backups / slug).glob("*") if p.is_dir() and "removed" not in p.name),
                        key=lambda p: p.stat().st_mtime)
        if not copies:
            raise UpdateError(f"Более ранней копии {slug} нет.")
        backup = copies[-1]
        self._restore(info["dir"], backup)
        old_version = backup.name.split("-")[0]
        self._quarantine(slug, info["version"], "rolled back by the person")
        self._log(slug, "skill", info["version"], old_version, "rolled_back", "by the person")
        self.hub.event("rolled_back", slug, old_version, "by the person")
        return {"ok": True, "slug": slug, "from": info["version"], "to": old_version}

    async def auto_apply(self) -> list[dict]:
        """Unattended updates the policy allows (trusted + clean + nothing new asked for)."""
        if self.policy.get("auto_update_skills") != "trusted":
            return []
        done = []
        for row in self.available():
            if row["needs_approval"] or self.quarantined(row["component"], row["latest"]):
                continue
            try:
                done.append({"component": row["component"], **(await self.apply(row["component"], approved=False, auto=True))})
            except Exception as exc:   # noqa: BLE001 — one failing update never stops the others
                done.append({"component": row["component"], "ok": False, "status": "failed", "message": str(exc)[:300]})
        return done
