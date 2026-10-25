"""The conversation's doors to OpenClaw, Hermes and the capability catalog (from jarvis-os capabilities/abilities.py).

jarvis-os had one `capability` tool whose risk depended on the action. JARVIS's rule is one risk per tool, so it is split:
looking is LOW; changing skills or connections asks first (MODERATE); running an upstream skill (OpenClaw's agent may
run commands on this computer), installing from ClawHub and sharing an agent-written skill with both agents are HIGH.
Every Hermes step that Hermes itself marks risky becomes a separate JARVIS approval card on the same task.
Agent output is untrusted data, never instructions.
"""
from __future__ import annotations

import asyncio
import json

from ..tasks import S
from ..tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification

REVIEWS: dict[str, dict] = {}     # ClawHub ref -> the install review shown on the approval card
BUILT: dict[str, dict] = {}       # skill name -> the last build (what sharing must match)

HERMES_STEP = ToolSpec(
    id="hermes_step", title="Шаг Hermes", description="A step Hermes asked to run during delegated work.",
    params={"type": "object", "properties": {}}, risk=Risk.HIGH, side_effects="local", model_callable=False,
    impact="Hermes выполнит этот шаг на компьютере")


def _hub(t) -> object:
    if t.hub is None:
        raise ToolError("«Возможности» не запущены")
    return t.hub


def approver(hub, ctx, label: str):
    """Hermes asking for a risky step mid-run → an ordinary JARVIS approval card on this task (HIGH, UI click)."""
    gw = hub.gateway

    async def approve(event: dict) -> bool:
        cmd = str(event.get("command") or "")[:1000]
        what = str(event.get("description") or "шаг")[:200]
        summary = {"title": f"{label}: {what}", "action": f"{label} хочет выполнить: {what}" + (f"\n{cmd}" if cmd else ""),
                   "reason": "Hermes сам пометил этот шаг как рискованный", "impact": HERMES_STEP.impact,
                   "risk": "high", "side_effects": "local", "editable": [], "session_grant_allowed": False}
        prev = hub.tasks.state(ctx.task_id)
        a = gw.approvals.create(ctx.task_id, ctx.step_id, HERMES_STEP, {"command": cmd, "description": what}, summary)
        if prev in (S.RUNNING, S.PLANNING):
            hub.tasks.transition(ctx.task_id, S.WAITING_APPROVAL)
        a = await gw.approvals.wait(a["id"])
        if hub.tasks.state(ctx.task_id) == S.WAITING_APPROVAL:
            hub.tasks.transition(ctx.task_id, prev if prev in (S.RUNNING, S.PLANNING) else S.RUNNING)
        return a["status"] == "approved" and not gw.policy.estop

    def progress(event: dict) -> None:
        what = "начал" if event.get("event") == "tool.started" else "закончил"
        ctx.progress(f"{label}: {what} {event.get('tool') or 'шаг'}", None)
    return approve, progress


class _HubTool(Tool):
    def __init__(self, hub):
        self.hub = hub


class CapabilityFind(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="capability_find", title="Найти возможность",
            description=("What can do a task beyond JARVIS's built-in tools: installed OpenClaw skills, plugins and "
                         "channels, Hermes skills, JARVIS's own skills and connections, and (mode=search) ClawHub, the "
                         "public skill registry. mode: find (installed, ranked), search (+ClawHub), info (one id), "
                         "review (a ClawHub release before installing: publisher, version, security scan, requirements), "
                         "list (everything by kind), status (agents' health), updates (check), history, assess "
                         "(compare all options for a new ability, e.g. a service JARVIS can't use yet). query: what is "
                         "needed, in English keywords (the catalog is in English). Use only ids it returned."),
            params={"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["find", "search", "info", "review", "list", "status", "updates",
                                                    "history", "assess"], "default": "find"},
                "query": {"type": "string", "maxLength": 400}, "id": {"type": "string", "maxLength": 200},
                "ref": {"type": "string", "maxLength": 200}, "version": {"type": "string", "maxLength": 40},
                "kind": {"type": "string", "enum": ["skills", "plugins", "channels", "apps", "tools", "all"],
                         "default": "all"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=60,
            verification="Каталог ответил; найденные id взяты из него")

    def describe(self, args):
        return {"search": "Поиск в ClawHub", "review": "Проверка навыка ClawHub", "status": "Состояние агентов",
                "updates": "Проверка обновлений", "assess": "Как научиться новому"}.get(args.get("mode"), "Поиск возможности") \
            + (f": «{(args.get('query') or args.get('ref') or args.get('id') or '')[:80]}»"
               if args.get("query") or args.get("ref") or args.get("id") else "")

    async def run(self, args, ctx):
        hub = _hub(self)
        mode, q = args.get("mode") or "find", str(args.get("query") or "")
        if mode in ("find", "search", "assess") and len(q) < 2:
            raise ToolInputError("Укажите, что нужно сделать (query)")
        if mode == "find":
            p = hub.find(q)
            return ToolResult({**p, "message": say_plan(p)}, say_plan(p), card="capabilities", untrusted=True)
        if mode == "search":
            p = await hub.search(q)
            n = len(p.get("registry") or [])
            msg = say_plan(p) + (f" В ClawHub подходящих навыков: {n}." if n else "")
            return ToolResult({**p, "message": msg}, msg, card="capabilities", untrusted=True)
        if mode == "info":
            c = hub.registry.find(str(args.get("id") or ""))
            if c is None:
                return ToolResult({"found": False}, "Такой возможности нет")
            return ToolResult({"capability": c.public()}, f"{c.title}: {c.description[:200]}", untrusted=True)
        if mode == "review":
            ref = str(args.get("ref") or args.get("id") or "")
            if not ref:
                raise ToolInputError("Укажите ref навыка ClawHub (@автор/имя)")
            if hub.lifecycle is None or hub.oc is None:
                raise ToolError("OpenClaw не подключён — проверить навык нельзя")
            r = await hub.lifecycle.review(ref, str(args.get("version") or ""))
            REVIEWS[r["ref"]] = REVIEWS[ref] = r
            req = ", ".join(r["requirements"]) or "ничего дополнительно"
            msg = (f"«{r['title']}» {r['version']} от {r['owner'] or 'неизвестного автора'}: проверка ClawHub — "
                   f"{r['scan']}; нужно: {req}.")
            return ToolResult({"review": r}, msg, untrusted=True)
        if mode == "list":
            snap = hub.snapshot()
            kind = args.get("kind") or "all"
            data = {k: v for k, v in snap.items() if isinstance(v, list) and (kind == "all" or k == kind)}
            rows = {k: [{"id": x["id"], "title": x["title"], "state": x["state"], "health": x["health"]} for x in v][:60]
                    for k, v in data.items()}
            s = snap["summary"]
            return ToolResult({**rows, "summary": s}, f"Возможностей: {s['total']}, готовы: {s['usable']}.")
        if mode == "status":
            b = hub.backends.snapshot()
            words = {True: "работает", False: "не работает"}
            msg = "; ".join(f"{b[n]['label'] if 'label' in b[n] else n}: "
                            + (words[bool(b[n].get("healthy"))] if b[n].get("installed") else "не установлен")
                            for n in ("openclaw", "hermes"))
            return ToolResult({"backends": b, "summary": hub.registry.summary()}, msg + ("; агенты остановлены вами"
                                                                                       if b.get("lockdown") else ""))
        if mode in ("updates", "history"):
            if hub.lifecycle is None or hub.oc is None:
                raise ToolError("OpenClaw не подключён")
            if mode == "history":
                h = hub.lifecycle.history(20)
                return ToolResult({"history": h}, f"Последних обновлений: {len(h)}")
            rows = await hub.lifecycle.check()
            return ToolResult({"updates": rows}, f"Доступно обновлений: {len(rows)}" if rows else "Всё обновлено")
        from .extend import assess
        a = await assess(hub, q, str(args.get("id") or ""))
        best = a["recommendation"]
        return ToolResult(a, f"Лучший вариант: {best['title']} ({best['method']})" if best else "Вариантов не нашлось",
                          untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified", "Ответ взят из каталога возможностей")


def say_plan(p: dict) -> str:
    c = p.get("choice")
    if p["action"] == "use" and c:
        return f"Подходит «{c['title']}» ({c['provider']}, {c['kind']}); id {c['id']}."
    if p["action"] == "connect" and c:
        return f"«{c['title']}» сможет, когда его подключат; id {c['id']}."
    if p["action"] == "enable" and c:
        return f"«{c['title']}» мог бы, но не готов: {c['state']}, {c['health']}."
    return "Установленного подходящего нет. Можно поискать в ClawHub (mode=search) или оценить варианты (mode=assess)."


class CapabilityUse(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="capability_use", title="Навык агента",
            description=("Run one OpenClaw or Hermes skill (an id from capability_find) on a request. OpenClaw's agent "
                         "may run commands on this computer, so this always asks the person first. Not for JARVIS's own "
                         "tools: call those directly."),
            params={"type": "object", "required": ["id", "request"], "properties": {
                "id": {"type": "string", "minLength": 3, "maxLength": 200},
                "request": {"type": "string", "minLength": 2, "maxLength": 4000}}},
            risk=Risk.HIGH, side_effects="external", timeout_s=None, idempotent=False,
            verification="Агент ответил; что он сделал за пределами ответа, JARVIS не проверяет",
            impact="Агент OpenClaw или Hermes выполнит навык (может запускать команды и ходить в сеть)")

    def describe(self, args):
        cap = self.hub.registry.find(str(args.get("id", ""))) if self.hub else None
        who = {"openclaw": "OpenClaw", "hermes": "Hermes", "shared": "Hermes"}.get(cap.provider, "") if cap else ""
        name = cap.title if cap else str(args.get("id"))
        return f"{name}{' (' + who + ')' if who else ''}: {str(args.get('request', ''))[:300]}"

    async def run(self, args, ctx):
        hub = _hub(self)
        approve, progress = approver(hub, ctx, str(args["id"]).split(":")[-1])
        hub.delegations[ctx.task_id] = ctx.step_id
        try:
            r = await hub.invoke(args["id"], args["request"], approve=approve, progress=progress)
        finally:
            hub.delegations.pop(ctx.task_id, None)
        if not r.get("ok"):
            raise ToolError(str(r.get("message") or "агент не справился")[:400])
        out = str(r.get("output") or r.get("message") or "")
        return ToolResult({"text": out[:8000], "executor": r.get("executor"), "capability": r.get("capability"),
                           "ms": r.get("ms"), "joined": bool(r.get("joined"))},
                          f"{r.get('executor', 'агент').title()} ответил за {round((r.get('ms') or 0) / 1000)} с",
                          untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("unverified", "Агент ответил; последствия его действий JARVIS не проверял")


class Specialist(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="specialist", title="Поручить Hermes",
            description=("Hand a longer multi-step task to Hermes, JARVIS's specialist agent (research and writing, "
                         "working through files, using its skills, memory and JARVIS's fast search), and get the result. "
                         "For open-ended work no single tool does — never for simple desktop actions or code changes "
                         "(those are claude_change / computer_task). Hermes runs with limited tools; every step it marks "
                         "risky asks the person again."),
            params={"type": "object", "required": ["task"], "properties": {
                "task": {"type": "string", "minLength": 3, "maxLength": 8000},
                "skill": {"type": "string", "maxLength": 80}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=None, idempotent=False,
            verification="Hermes завершил работу и вернул ответ",
            impact="Hermes работает с файлами, навыками и памятью; рискованные шаги — только после вашего «да»")

    def describe(self, args):
        return f"Hermes: {str(args.get('task', ''))[:400]}"

    async def run(self, args, ctx):
        hub = _hub(self)
        h = hub.hermes
        if h is None:
            raise ToolError("Hermes не запущен (Возможности → Агенты)")
        if hub.backends.lockdown:
            raise ToolError("Агенты остановлены вами — сначала «возобнови агентов»")
        approve, progress = approver(hub, ctx, "Hermes")
        instructions = ("Answer in the language of the task. " +
                        (f"Follow your skill '{args['skill']}' (skill_view)." if args.get("skill") else ""))
        hub.delegations[ctx.task_id] = ctx.step_id
        try:
            r = await h.delegate(args["task"], title=args["task"][:80], instructions=instructions, approve=approve,
                                 progress=progress, timeout=self.hub.cfg.hermes_timeout_s or None)
        finally:
            hub.delegations.pop(ctx.task_id, None)
        if not r["ok"]:
            raise ToolError(f"Hermes не справился: {r['error'] or r['status']}"[:400])
        return ToolResult({"text": r["output"][:8000], "tools": r["tools"], "run_id": r["run_id"], "status": r["status"]},
                          f"Hermes закончил: шагов {len(r['tools'])}", untrusted=True)

    async def verify(self, args, result, ctx):
        if result.data.get("status") == "completed" and result.data.get("text"):
            return Verification("verified", f"Запуск {result.data['run_id']} завершён с ответом")
        return Verification("unverified", "Hermes закончил без текста ответа")


class SkillInstall(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="skill_install", title="Установить навык",
            description=("Install a skill from ClawHub into OpenClaw — only after capability_find mode=review for the "
                         "same ref (the approval card shows that review). Releases whose security scan is not clean are "
                         "refused; the installed SKILL.md must match the reviewed one."),
            params={"type": "object", "required": ["ref"], "properties": {
                "ref": {"type": "string", "minLength": 2, "maxLength": 200},
                "version": {"type": "string", "maxLength": 40}}},
            risk=Risk.HIGH, side_effects="external", timeout_s=240, idempotent=False,
            verification="OpenClaw показывает навык этой версии, и его текст совпадает с одобренным",
            impact="В OpenClaw появится новый навык из публичного каталога ClawHub")

    def describe(self, args):
        r = REVIEWS.get(str(args.get("ref", "")))
        if r is None:
            return f"Установить навык {args.get('ref')} из ClawHub (проверка ещё не показана — установка откажет)"
        req = ", ".join(r["requirements"]) or "ничего дополнительно"
        warn = f" Предупреждения: {'; '.join(r['warnings'])}." if r["warnings"] else ""
        return (f"Установить «{r['title']}» ({r['ref']}) {r['version']} от {r['owner'] or 'неизвестного автора'} из "
                f"ClawHub в OpenClaw. Проверка безопасности: {r['scan']}. Нужно: {req}. {r['summary'][:200]}{warn}")

    async def run(self, args, ctx):
        hub = _hub(self)
        if hub.lifecycle is None or hub.oc is None:
            raise ToolError("OpenClaw не подключён — устанавливать навыки сейчас нельзя")
        ref = str(args["ref"])
        shown = REVIEWS.get(ref)
        if shown is None:
            raise ToolError("Сначала покажите проверку навыка (capability_find mode=review) — без неё не устанавливаю")
        fresh = await hub.lifecycle.review(ref, str(args.get("version") or shown["version"]))
        if fresh.get("card_sha") != shown.get("card_sha") or fresh["version"] != shown["version"]:
            raise ToolError("Навык в ClawHub изменился после показанной проверки — проверьте его заново")
        r = await hub.lifecycle.install(ref, shown)
        await hub.registry.refresh("openclaw")
        cap = hub.registry.find(r["slug"], provider="openclaw")
        missing = "; ".join(cap.requires.get("missing", [])) if cap else ""
        msg = f"Установлен {r['slug']} {r['version']}." + ("" if r["eligible"] else f" OpenClaw пока не может им пользоваться: {missing}")
        return ToolResult({**r, "callable": bool(cap and cap.usable)}, msg)

    async def verify(self, args, result, ctx):
        inst = await self.hub.lifecycle.installed()
        row = inst.get(result.data["slug"])
        if row and row["version"] == result.data["version"]:
            return Verification("verified", f"OpenClaw показывает {result.data['slug']} {row['version']}")
        return Verification("failed", "OpenClaw не показывает установленный навык")


class SkillManage(_HubTool):
    ACTIONS = ("enable", "disable", "uninstall", "update", "rollback", "unshare")

    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="skill_manage", title="Управление навыком",
            description=("Change an installed agent skill: enable, disable, uninstall (a copy is kept), update (staged, "
                         "smoke-tested, rolled back on failure), rollback (to the copy before the last update), unshare "
                         "(take a shared skill away from both agents). id: the skill slug or capability id."),
            params={"type": "object", "required": ["action", "id"], "properties": {
                "action": {"type": "string", "enum": list(self.ACTIONS)},
                "id": {"type": "string", "minLength": 2, "maxLength": 200}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=240, idempotent=False,
            verification="OpenClaw показывает новое состояние навыка",
            impact="Меняется набор навыков агентов")

    def describe(self, args):
        words = {"enable": "Включить", "disable": "Выключить", "uninstall": "Удалить (копия сохранится)",
                 "update": "Обновить (с проверкой и откатом)", "rollback": "Откатить", "unshare": "Убрать у агентов"}
        return f"{words.get(args.get('action'), args.get('action'))} навык {args.get('id')}"

    @staticmethod
    def slug(raw: str) -> str:
        return raw.split(":")[-1].lstrip("@").split("/")[-1]

    async def run(self, args, ctx):
        hub = _hub(self)
        action, slug = args["action"], self.slug(args["id"])
        if action == "unshare":
            from .extend import remove_shared
            r = remove_shared(hub, slug)
            await hub.registry.refresh("shared")
            if not r["ok"]:
                raise ToolError(r["message"])
            return ToolResult(r, r["message"])
        lc = hub.lifecycle
        if lc is None or hub.oc is None:
            raise ToolError("OpenClaw не подключён")
        from .updates import UpdateError
        try:
            if action in ("enable", "disable"):
                r = await lc.set_enabled(slug, action == "enable")
                msg = f"Навык {slug} {'включён' if action == 'enable' else 'выключен'}"
            elif action == "uninstall":
                r = await lc.uninstall(slug)
                msg = f"Навык {slug} удалён (копия: {r['kept']})"
            elif action == "update":
                r = await lc.apply(slug, approved=True)
                msg = r.get("message") or f"{slug} обновлён: {r.get('from')} → {r.get('to')}"
            else:
                r = await lc.rollback(slug)
                msg = f"{slug} возвращён к версии {r['to']}"
        except UpdateError as e:
            raise ToolError(str(e)) from None
        await hub.registry.refresh("openclaw")
        if not r.get("ok", True):
            raise ToolError(msg)
        return ToolResult({**r, "action": action, "slug": slug}, msg)

    async def verify(self, args, result, ctx):
        action, slug = args["action"], result.data.get("slug")
        if action == "unshare":
            ok = not (self.hub.backends.layout.shared_skills / slug / "SKILL.md").exists()
            return Verification("verified" if ok else "failed", "Общего SKILL.md больше нет" if ok else "Файл остался")
        inst = await self.hub.lifecycle.installed()
        row = inst.get(slug)
        if action == "uninstall":
            return Verification("verified" if row is None else "failed",
                                "OpenClaw больше не показывает навык" if row is None else "Навык всё ещё в списке")
        if action in ("enable", "disable"):
            want = action == "disable"
            ok = row is not None and row["disabled"] == want
            return Verification("verified" if ok else "failed", "Состояние в OpenClaw совпадает" if ok else "Не совпадает")
        if row and result.data.get("to") and row["version"] == result.data["to"]:
            return Verification("verified", f"OpenClaw показывает версию {row['version']}")
        return Verification("unverified", "Версию после изменения подтвердить не удалось")


class ServiceConnect(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.bus = None
        self.spec = ToolSpec(
            id="service_connect", title="Подключить канал",
            description=("Connect a chat channel through OpenClaw (telegram bot, whatsapp, discord, slack, signal): shows "
                         "the connection card in the window. The token or QR step is done by the person there — never "
                         "ask them to say or type a token in the conversation. JARVIS's own Telegram/WhatsApp/mail "
                         "accounts are connected in their workspaces instead (window_show connect=true)."),
            params={"type": "object", "required": ["service"], "properties": {
                "service": {"type": "string", "minLength": 2, "maxLength": 40}}},
            risk=Risk.LOW, side_effects="none", timeout_s=20,
            verification="Карточка подключения показана в окне")

    def describe(self, args):
        return f"Подключить {args.get('service')} через OpenClaw"

    async def run(self, args, ctx):
        hub = _hub(self)
        p = await hub.connect_plan(args["service"])
        if p["via"] != "openclaw":
            return ToolResult(p, p["needs"])
        if p.get("blocked"):
            raise ToolError(p["blocked"])
        hub.bus.publish("ui.navigate", {"view": "abilities"})
        hub.bus.publish("hub.connect", {"plan": p})
        msg = f"Открыл карточку подключения {p['label']}: нужен {p['needs']}." if p["flow"] == "token" else \
            f"{p['label']}: нужен {p['needs']}. Команда для терминала есть в карточке."
        return ToolResult(p, msg)

    async def verify(self, args, result, ctx):
        return Verification("verified" if result.data.get("via") == "openclaw" else "unverified",
                            "Карточка отправлена в окно" if result.data.get("via") == "openclaw" else "Подключения нет")


class ServiceDisconnect(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="service_disconnect", title="Отключить канал",
            description="Disconnect a chat channel connected through OpenClaw and remove its stored token.",
            params={"type": "object", "required": ["service"], "properties": {
                "service": {"type": "string", "enum": ["telegram", "whatsapp", "discord", "slack", "signal"]}}},
            risk=Risk.MODERATE, side_effects="external", timeout_s=60, idempotent=True,
            verification="OpenClaw показывает канал отключённым", impact="Канал перестанет принимать и отправлять сообщения")

    def describe(self, args):
        return f"Отключить {args.get('service')} в OpenClaw и удалить его токен"

    async def run(self, args, ctx):
        r = await _hub(self).disconnect_channel(args["service"])
        if not r["ok"]:
            raise ToolError(r["message"])
        return ToolResult(r, r["message"])

    async def verify(self, args, result, ctx):
        rows = [c for c in await self.hub.oc.channels() if c["channel"] == args["service"]] if self.hub.oc else []
        ok = not rows or rows[0]["state"] in ("disabled", "not_configured", "disconnected")
        return Verification("verified" if ok else "failed", "Канал отключён" if ok else "Канал всё ещё подключён")


class SkillBuild(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="skill_build", title="Написать навык",
            description=("When nothing exists for an ability (capability_find mode=assess said so): Hermes writes a new "
                         "skill in its development folder, JARVIS reviews it, and Hermes test-runs it once on a harmless "
                         "sample. The skill stays «в проверке» until the person shares it (skill_share)."),
            params={"type": "object", "required": ["request"], "properties": {
                "request": {"type": "string", "minLength": 5, "maxLength": 2000},
                "name": {"type": "string", "maxLength": 48}, "sample": {"type": "string", "maxLength": 1000}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=None, idempotent=False,
            verification="Файл навыка создан, разобран и прошёл пробный запуск",
            impact="Hermes создаст навык в своей папке разработки (никому не доступен до вашего решения)")

    def describe(self, args):
        return f"Hermes напишет и проверит навык: {str(args.get('request', ''))[:300]}"

    async def run(self, args, ctx):
        hub = _hub(self)
        from .extend import build
        approve, progress = approver(hub, ctx, "Hermes")
        hub.delegations[ctx.task_id] = ctx.step_id
        try:
            r = await build(hub, args["request"], name=str(args.get("name") or ""), sample=str(args.get("sample") or ""),
                            approve=approve, progress=progress)
        finally:
            hub.delegations.pop(ctx.task_id, None)
        if r.get("name"):
            BUILT[r["name"]] = r
        if not r.get("name"):
            raise ToolError(r.get("message", "Навык не создан"))
        return ToolResult(r, r["message"], card="skill_review", untrusted=True)

    async def verify(self, args, result, ctx):
        d = result.data
        if d.get("ok"):
            return Verification("verified", f"SKILL.md разобран, пробный запуск ответил (sha {d['sha256'][:10]})")
        return Verification("failed", "; ".join(d.get("problems") or ["проверка не пройдена"]))


class SkillShare(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="skill_share", title="Сделать навык общим",
            description=("Share a skill Hermes built and tested in this session with both agents (OpenClaw and Hermes). "
                         "The approval card shows its full text and review flags; a skill that changed since is refused."),
            params={"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "minLength": 3, "maxLength": 48}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=30, idempotent=True,
            verification="В общей папке лежит ровно тот текст, что вы одобрили",
            impact="Оба агента начнут пользоваться этим навыком")

    def describe(self, args):
        b = BUILT.get(str(args.get("name", "")))
        if b is None:
            return f"Сделать общим навык {args.get('name')} (он не собирался в этом сеансе — откажу)"
        flags = f" Замечания проверки: {', '.join(b['flags'])}." if b["flags"] else " Замечаний нет."
        return f"Сделать навык {b['name']} доступным OpenClaw и Hermes.{flags}\n---\n{b['skill_md'][:3000]}"

    async def run(self, args, ctx):
        hub = _hub(self)
        from .extend import promote
        b = BUILT.get(args["name"])
        if b is None or not b.get("ok"):
            raise ToolError("Общим можно сделать только навык, собранный и проверенный в этом сеансе")
        r = promote(hub, b["name"], b["sha256"])
        await hub.registry.refresh("shared", "hermes")
        if not r["ok"]:
            raise ToolError(r["message"])
        return ToolResult({**r, "sha256": b["sha256"]}, r["message"])

    async def verify(self, args, result, ctx):
        import hashlib
        p = self.hub.backends.layout.shared_skills / result.data["name"] / "SKILL.md"
        ok = p.exists() and hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() == result.data["sha256"]
        return Verification("verified" if ok else "failed", "Текст совпадает с одобренным" if ok else "Текст отличается")


class AgentsStop(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="agents_stop", title="Остановить агентов",
            description="Stop every OpenClaw and Hermes run now and refuse new ones until the person resumes them.",
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="local", timeout_s=30,
            verification="Ни одного запуска агентов не осталось")

    async def run(self, args, ctx):
        r = await _hub(self).backends.stop_all()
        return ToolResult(r, f"Агенты остановлены: OpenClaw {r['openclaw_runs']}, Hermes {r['hermes_runs']}. "
                             "Новые запуски — только после «возобнови агентов».")

    async def verify(self, args, result, ctx):
        b = self.hub.backends
        left = (len(b.hermes.runs) if b.hermes else 0) + (len(b.openclaw.runs) if b.openclaw else 0)
        return Verification("verified" if b.lockdown and not left else "failed", f"Запусков осталось: {left}")


class AgentsResume(_HubTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="agents_resume", title="Возобновить агентов",
            description="Let OpenClaw and Hermes take work again after agents_stop.",
            params={"type": "object", "properties": {}}, risk=Risk.MODERATE, side_effects="local", timeout_s=10,
            verification="Остановка агентов снята", impact="Агенты снова смогут выполнять поручения")

    async def run(self, args, ctx):
        _hub(self).backends.lift()
        return ToolResult({}, "Агенты снова принимают поручения.")

    async def verify(self, args, result, ctx):
        return Verification("verified" if not self.hub.backends.lockdown else "failed", "Остановка снята")


def tools(hub) -> list[Tool]:
    return [CapabilityFind(hub), CapabilityUse(hub), Specialist(hub), SkillInstall(hub), SkillManage(hub),
            ServiceConnect(hub), ServiceDisconnect(hub), SkillBuild(hub), SkillShare(hub), AgentsStop(hub),
            AgentsResume(hub)]


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


__all__ = ["tools", "REVIEWS", "BUILT", "approver", "asyncio"]
