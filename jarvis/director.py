"""Director: receives user requests, picks the cheapest reliable execution path,
runs it as a persistent task and keeps one canonical runtime state."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from . import router as rt
from .db import Database, dumps, loads, new_id, now
from .events import AuditLog, EventBus
from .gateway import Gateway, Outcome
from .policy import ApprovalError, Approvals, PolicyEngine
from .scheduler import Scheduler, fmt_time
from .tasks import S, TERMINAL, IllegalTransition, TaskStore
from .tools.base import Risk, Tool, ToolResult, ToolSpec, Verification

COMPLETED_FLASH_S = 2.5
_STATE_KINDS = {"approval.created", "approval.updated", "step.updated", "task.updated", "task.created"}


class CloudConsent(Tool):
    """Pseudo-tool so that, in PRIVATE_LOCAL mode, sending a request to the cloud goes
    through the same approval path as any other consequential action."""
    spec = ToolSpec(id="cloud_send", title="Отправка в облачный ИИ",
                    description="Send the user's request to Claude", params={"type": "object", "properties": {
                        "provider": {"type": "string"}, "preview": {"type": "string", "maxLength": 400}}},
                    risk=Risk.MODERATE, side_effects="external", timeout_s=2, model_callable=False,
                    verification="Согласие пользователя зафиксировано",
                    impact="Текст запроса и недавний контекст беседы уйдут в Claude (Anthropic) через Claude Code.")

    def describe(self, args):
        return f"Отправить запрос в {args.get('provider', 'Claude')}: «{args.get('preview', '')[:120]}»"

    async def run(self, args, ctx):
        return ToolResult({}, "Отправка разрешена.")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


@dataclass
class Runtime:
    task: asyncio.Task | None = None
    gate: asyncio.Event = field(default_factory=lambda: _set_event())
    thinking: bool = False
    message_id: str | None = None
    voice: bool = False          # answer is spoken (voice turn or "speak replies")
    turn: str | None = None      # voice telemetry id


def _set_event() -> asyncio.Event:
    e = asyncio.Event()
    e.set()
    return e


class Director:
    def __init__(self, *, db: Database, bus: EventBus, audit: AuditLog, tasks: TaskStore, policy: PolicyEngine,
                 approvals: Approvals, gateway: Gateway, ai, scheduler: Scheduler, config,
                 flora=None, providers=None, agency=None):
        self.db, self.bus, self.audit, self.tasks = db, bus, audit, tasks
        self.policy, self.approvals, self.gw, self.ai = policy, approvals, gateway, ai
        self.scheduler, self.config, self.flora = scheduler, config, flora
        self.providers, self.agency = providers, agency
        self.speech = None           # SpeechManager, set by the composition root
        self.stt = None              # LocalSTT (speech recognition on this computer)
        self.comms = None            # CommsAgent
        self.hub = None              # CommsHub
        self.rt: dict[str, Runtime] = {}
        self._last_finish: tuple[float, str] | None = None
        self._state = None
        self._flash_handle = None
        self.started_at = time.time()
        self.policy.estop = bool(db.get_kv("estop", False))
        self._base_scopes = (list(config.read_scopes), list(config.code_workspaces))
        self.set_access(db.get_kv("access_level") or "standard", quiet=True)
        scheduler.on_fire = self._on_reminder
        bus.listeners.append(lambda kind, ev=None: self._touch() if kind in _STATE_KINDS else None)
        bus.listeners.append(self._speak_approval)

    # ------------------------------------------------------------------ conversations
    def conversation_id(self) -> str:
        cid = self.db.get_kv("current_conversation")
        if cid and self.db.one("SELECT 1 FROM conversations WHERE id=?", (cid,)):
            return cid
        return self.new_conversation()

    def new_conversation(self) -> str:
        cid = new_id("c_")
        t = now()
        self.db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                        (cid, None, t, t))
        self.db.set_kv("current_conversation", cid)
        self.bus.publish("conversation.new", {"conversation_id": cid})
        return cid

    def ctx(self, cid: str) -> dict:
        return self.db.get_kv("ctx:" + cid, {}) or {}

    def save_ctx(self, cid: str, ctx: dict) -> None:
        self.db.set_kv("ctx:" + cid, ctx)

    def add_message(self, cid: str, role: str, text: str, task_id: str | None = None, meta: dict | None = None,
                    mid: str | None = None) -> dict:
        mid = mid or new_id("m_")
        t = now()
        self.db.execute("INSERT INTO messages(id,conversation_id,role,text,task_id,meta,created_at)"
                        " VALUES(?,?,?,?,?,?,?)", (mid, cid, role, text, task_id, dumps(meta or {}), t))
        self.db.execute("UPDATE conversations SET updated_at=?, title=COALESCE(title, ?) WHERE id=?",
                        (t, text[:80] if role == "user" else None, cid))
        msg = {"id": mid, "conversation_id": cid, "role": role, "text": text, "task_id": task_id,
               "meta": meta or {}, "created_at": t}
        self.bus.publish("message", {"message": msg}, task_id)
        return msg

    def messages(self, cid: str, limit: int = 80) -> list[dict]:
        rows = self.db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?",
                           (cid, limit))
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["meta"] = loads(d["meta"], {})
            out.append(d)
        return out

    # ------------------------------------------------------------------ entry point
    async def submit(self, text: str, source: str = "text", turn: str | None = None, speak: bool = False) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty")
        text = text[:8000]
        cid = self.conversation_id()
        # "Да" / "Нет" to the message JARVIS just asked to confirm (voice or typed in the window; never via the API).
        if self.comms and source in ("voice", "text"):
            from .comms.agent import yes_no
            yn = yes_no(text)
            pend = [a for a in self.approvals.pending() if self.voice_approvable(a["tool_id"])] if yn is not None else []
            if len(pend) == 1:
                self.add_message(cid, "user", text, pend[0]["task_id"], meta={"source": source, "decision": yn})
                if self.speech and turn:
                    self.speech.annotate(turn, route="approval", transcript=text, task_id=pend[0]["task_id"])
                r = self.rt.get(pend[0]["task_id"])
                if r and source == "voice":
                    r.voice, r.turn = True, turn or r.turn
                self.audit.write("approval.by_" + source, approval_id=pend[0]["id"], approve=yn)
                self.decide(pend[0]["id"], yn)
                return pend[0]["task_id"]
        ctx = self.ctx(cid)
        route = rt.route(text, ctx)
        if self.comms and (route is None or route.kind not in ("control",)) and self.comms.answers_pending(text, ctx):
            route = rt.Route("comms", args={"text": text}, why="Ответ на уточнение JARVIS", level=1)
        # Control commands act immediately and are not tasks themselves.
        if route and route.kind == "control":
            self.add_message(cid, "user", text, meta={"source": source})
            reply = await self.control(route.args["op"])
            if self.speech and turn:
                self.speech.annotate(turn, route="control:" + route.args["op"], transcript=text)
            if route.args["op"] not in ("new_conversation", "interrupt"):   # silence / empty screen is the answer
                self.add_message(self.conversation_id(), "assistant", reply, meta={"route": "control", "level": 0})
            return ""
        level = route.level if route else 2
        tid = self.tasks.create(text, cid, route=(route.kind if route else "ai"), level=level)
        self.add_message(cid, "user", text, tid, meta={"source": source})
        runtime = Runtime(voice=source == "voice" or speak, turn=turn)
        self.rt[tid] = runtime
        if runtime.voice and self.speech:
            self.speech.start(tid, turn, max_chars=1200 if route is None else 400)
            self.speech.annotate(turn, task_id=tid, transcript=text, route=route.kind if route else "ai")
        runtime.task = asyncio.create_task(self._run(tid, cid, text, route), name=f"task-{tid}")
        self._touch()
        return tid

    async def run_ui_tool(self, tool: str, args: dict) -> str:
        """A button in the UI asked for a tool directly (no text command)."""
        cid = self.conversation_id()
        spec = self.gw.registry.get(tool).spec
        label = spec.title + (": " + os.path.basename(str(args.get("path", ""))) if args.get("path") else "")
        tid = self.tasks.create(label, cid, route="ui", level=0)
        self.rt[tid] = Runtime()
        route = rt.Route("tool", tool, args, why="Нажатие в интерфейсе")
        self.rt[tid].task = asyncio.create_task(self._run(tid, cid, label, route, announce=True))
        return tid

    async def _run(self, tid: str, cid: str, text: str, route: rt.Route | None, announce: bool = False) -> None:
        try:
            if announce:
                self.add_message(cid, "user", text, tid, meta={"source": "ui"})
            self.tasks.transition(tid, S.RUNNING, objective=(route.why if route else "Запрос к ИИ"))
            if route is None:
                await self.agency.run(self, tid, cid, text)
            elif route.kind == "reply":
                self._finish(tid, cid, self._reply(route.reply), meta={"route": "local", "level": 0})
            elif route.kind == "answer":
                self._finish(tid, cid, route.reply, meta={"route": "local", "level": 0, "why": route.why})
            elif route.kind == "tool":
                await self._run_tool_route(tid, cid, route)
            elif route.kind == "search_open":
                await self._run_search_open(tid, cid, route)
            elif route.kind == "comms":
                if self.comms is None:
                    self._finish(tid, cid, "Модуль связи не подключён в этой сборке.", meta={"route": "local"})
                else:
                    await self.comms.handle(self, tid, cid, text)
            elif route.kind == "flora":
                if self.flora is None:
                    self._finish(tid, cid, "Сценарий «Флора» ещё не подключён в этой сборке.",
                                 meta={"route": "local"})
                else:
                    await self.flora.handle(self, tid, cid, text)
        except asyncio.CancelledError:
            self._safe_transition(tid, S.CANCELLED, error="Отменено пользователем")
            self.add_message(cid, "assistant", "Задача отменена.", tid, meta={"route": "control"})
        except Exception as e:   # noqa: BLE001 — a task crash must never take down the service
            self.audit.write("task.crash", task_id=tid, error=repr(e))
            self._safe_transition(tid, S.FAILED, error=f"Внутренняя ошибка: {e.__class__.__name__}")
            self.add_message(cid, "assistant", "Произошла внутренняя ошибка, задача остановлена. Подробности в журнале.",
                             tid, meta={"route": "error"})
        finally:
            self.rt.pop(tid, None)
            st = self.tasks.state(tid)
            self._last_finish = (time.time(), "error" if st == S.FAILED else "ok")
            self._touch()
            loop = asyncio.get_running_loop()
            if self._flash_handle:
                self._flash_handle.cancel()
            self._flash_handle = loop.call_later(COMPLETED_FLASH_S + 0.05, self._touch)

    def _safe_transition(self, tid: str, s: S, **kw) -> None:
        try:
            if self.tasks.state(tid) not in TERMINAL:
                self.tasks.transition(tid, s, **kw)
        except IllegalTransition:
            self.tasks.update(tid, state=s.value, **kw)

    def _finish(self, tid: str, cid: str, text: str, meta: dict | None = None, failed: bool = False,
                result: dict | None = None, mid: str | None = None) -> None:
        self.add_message(cid, "assistant", text, tid, meta=meta, mid=mid)
        if failed:
            self._safe_transition(tid, S.FAILED, error=text[:500], result=result)
        else:
            self._safe_transition(tid, S.COMPLETED, result=result or {"text": text[:2000]})

    # ------------------------------------------------------------------ helpers for routes
    def _gate(self, tid: str):
        async def gate():
            r = self.rt.get(tid)
            if r and not r.gate.is_set():
                await r.gate.wait()
        return gate

    def _cancelled(self, tid: str):
        def check():
            r = self.rt.get(tid)
            return bool(r and r.task and (r.task.cancelling() > 0 or r.task.cancelled()))
        return check

    async def execute(self, tid: str, cid: str, tool: str, args: dict, why: str) -> Outcome:
        ctx = self.ctx(cid)
        out = await self.gw.execute(tid, tool, args, why, conversation=ctx, gate=self._gate(tid),
                                    cancelled=self._cancelled(tid))
        self.save_ctx(cid, ctx)
        self._touch()
        return out

    @staticmethod
    def outcome_text(out: Outcome) -> str:
        if out.status == "ok":
            txt = out.result.summary
            if out.verification and out.verification.status == "unverified":
                txt += f"\n\n_Не подтверждено: {out.verification.detail}._"
            return txt
        if out.status == "denied":
            return f"Не выполнено: {out.error}."
        return f"Не получилось: {out.error}"

    async def _run_tool_route(self, tid, cid, route: rt.Route) -> None:
        out = await self.execute(tid, cid, route.tool, route.args, route.why)
        self._finish(tid, cid, self.outcome_text(out), meta={"route": "local", "level": route.level,
                                                              "why": route.why, "cloud": False},
                     failed=out.status in ("failed", "invalid"),
                     result={"status": out.status, "step_id": out.step_id})

    async def _run_search_open(self, tid, cid, route: rt.Route) -> None:
        out = await self.execute(tid, cid, "search_files", route.args, route.why)
        if out.status != "ok":
            return self._finish(tid, cid, self.outcome_text(out), failed=True, meta={"route": "local"})
        hits = out.result.data["results"]
        if len(hits) == 1:
            out2 = await self.execute(tid, cid, "open_path", {"path": hits[0]["path"]}, "Единственный результат поиска")
            return self._finish(tid, cid, self.outcome_text(out2), meta={"route": "local"},
                                failed=out2.status in ("failed", "invalid"))
        if not hits:
            return self._finish(tid, cid, out.result.summary, meta={"route": "local"})
        self._finish(tid, cid, out.result.summary + " Какой открыть? Скажите, например, «открой второй».",
                     meta={"route": "local"})

    def _reply(self, kind: str) -> str:
        if kind == "greeting":
            h = time.localtime().tm_hour
            part = "Доброе утро" if 5 <= h < 12 else "Добрый день" if h < 18 else "Добрый вечер" if h < 23 else "Доброй ночи"
            return f"{part}. Чем помочь?"
        if kind == "time":
            return time.strftime("Сейчас %H:%M, ") + _ru_date(time.localtime()) + "."
        if kind == "tasks":
            act = self.tasks.active()
            if not act:
                return "Активных задач нет."
            lines = [f"- {t['request'][:60]} — {STATE_RU.get(t['state'], t['state'])}" for t in act]
            return f"Активных задач: {len(act)}.\n" + "\n".join(lines)
        if kind == "help":
            names = [p.display_name for pid, p in self.providers.providers.items()
                     if self.providers.health(pid).value in ("CONNECTED", "DEGRADED")] if self.providers else []
            ai = ("подключены: " + ", ".join(names)) if names else "не подключены (локальные команды работают без них)"
            return ("Вот что я делаю сам, без облака:\n"
                    "- **Система**: «состояние системы», «сколько места на диске»\n"
                    "- **Приложения**: «открой браузер / терминал / файлы / калькулятор»\n"
                    "- **Рабочие столы и окна**: «переключись на 3», «какие окна открыты»\n"
                    "- **Звук**: «громкость 40», «тише», «выключи звук»\n"
                    "- **Файлы**: «найди файл смета», затем «открой второй»; «открой загрузки»\n"
                    "- **Заметки и напоминания**: «запиши заметку: …», «напомни через 10 минут …»\n"
                    "- **Снимок экрана** — после вашего подтверждения\n"
                    "- **Флора**: «что важного по Флоре?» (демо-данные)\n\n"
                    f"ИИ-специалисты {ai}. Управление: «пауза», «продолжи», «отмени», «стоп» — экстренная остановка.")
        return ""

    def _checkpoint(self, tid, contents, rounds, model) -> None:
        try:
            ser = [c.model_dump(mode="json", exclude_none=True) for c in contents]
            blob = dumps({"round": rounds, "model": model, "contents": ser})
            if len(blob) < 300_000:
                self.tasks.update(tid, checkpoint=blob)
        except Exception:   # noqa: BLE001 — checkpoint is best effort
            pass

    # ------------------------------------------------------------------ controls
    async def control(self, op: str, task_id: str | None = None) -> str:
        if op == "estop":
            return await self.estop()
        if op == "estop_clear" or op == "resume_all" and self.policy.estop:
            self.policy.estop = False
            self.db.set_kv("estop", False)
            self.audit.write("estop.cleared")
            self.bus.publish("estop", {"active": False})
            if op == "resume_all":
                self._resume_all()
            self._touch()
            return "Экстренная остановка снята. Можно продолжать."
        if op in ("stop_speech", "interrupt"):
            self.bus.publish("speech.stop", {})
            if self.speech:
                self.speech.cancel()
            if op == "stop_speech":
                return "Молчу."
            # Drop answers in progress that change nothing on the computer; never interrupt real actions.
            readonly = {"agent_chat", "agent_generate", "claude_code_inspect", "search_files", "read_file", "system_info",
                        "window_list", "flora_inbox", "flora_compare"}
            for tid, r in list(self.rt.items()):
                t = self.tasks.get(tid)
                if not t or S(t["state"]) not in (S.RUNNING, S.PLANNING):
                    continue
                if all(s["tool_id"] in readonly for s in t["steps"]):
                    await self.cancel(tid)
            return ""
        if op == "new_conversation":
            self.new_conversation()
            return "Начинаем новый разговор."
        if op == "cancel_last":
            act = [t for t in self.tasks.active()]
            if not act:
                return "Нечего отменять."
            return await self.cancel(act[-1]["id"])
        if op == "cancel":
            return await self.cancel(task_id)
        if op == "pause":
            return self.pause(task_id)
        if op == "resume":
            return self.resume(task_id)
        if op == "pause_all":
            n = sum(1 for t in list(self.rt) if self.pause(t, quiet=True))
            return f"Пауза: {n} {'задача' if n == 1 else 'задачи'}." if n else "Нет задач, которые можно приостановить."
        if op == "resume_all":
            n = self._resume_all()
            return f"Продолжаю ({n})." if n else "Нечего продолжать."
        return "Неизвестная команда управления."

    def _resume_all(self) -> int:
        return sum(1 for t in list(self.rt) if self.resume(t, quiet=True))

    async def cancel(self, tid: str | None) -> str:
        r = self.rt.get(tid) if tid else None
        if r and r.task and not r.task.done():
            r.gate.set()
            # Close any open approval for this task so the waiter unblocks.
            for a in self.approvals.pending():
                if a["task_id"] == tid:
                    self.approvals.decide(a["id"], False)
            r.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(r.task), 3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self.audit.write("task.cancelled", task_id=tid)
            return "Отменил."
        t = self.tasks.get(tid) if tid else None
        if t and S(t["state"]) not in TERMINAL:
            # e.g. a task blocked by a restart or waiting on a persisted approval
            for a in self.approvals.pending():
                if a["task_id"] == tid:
                    self.approvals.decide(a["id"], False)
            self._safe_transition(tid, S.CANCELLED, error="Отменено пользователем")
            self._touch()
            return "Отменил."
        return "Эта задача уже завершена."

    def pause(self, tid: str | None, quiet: bool = False) -> bool | str:
        r = self.rt.get(tid) if tid else None
        if not r or not r.gate.is_set():
            return False if quiet else "Эту задачу сейчас нельзя приостановить."
        st = self.tasks.state(tid)
        if st not in (S.RUNNING, S.PLANNING):
            return False if quiet else "Задача ждёт вашего решения — её можно отменить."
        r.gate.clear()
        self.tasks.transition(tid, S.PAUSED)
        self.audit.write("task.paused", task_id=tid)
        self._touch()
        return True if quiet else "Пауза. Текущий шаг завершится, следующий не начнётся."

    def resume(self, tid: str | None, quiet: bool = False) -> bool | str:
        r = self.rt.get(tid) if tid else None
        if not r or r.gate.is_set():
            return False if quiet else "Эта задача не на паузе."
        self.tasks.transition(tid, S.RUNNING)
        r.gate.set()
        self.audit.write("task.resumed", task_id=tid)
        self._touch()
        return True if quiet else "Продолжаю."

    async def estop(self) -> str:
        self.policy.estop = True
        self.db.set_kv("estop", True)
        self.bus.publish("speech.stop", {})
        denied = self.approvals.cancel_all("Экстренная остановка")
        running = [t for t, r in self.rt.items() if r.task and not r.task.done()]
        for tid in running:
            r = self.rt.get(tid)
            if r:
                r.gate.set()
                r.task.cancel()
        if running:
            await asyncio.wait([self.rt[t].task for t in running if t in self.rt], timeout=3)
        still = [t for t in running if t in self.rt and not self.rt[t].task.done()]
        self.audit.write("estop.engaged", cancelled=running, approvals_closed=denied, not_stopped=still)
        self.bus.publish("estop", {"active": True, "cancelled": len(running), "approvals_closed": denied})
        self._touch()
        msg = f"Экстренная остановка. Остановлено задач: {len(running)}, закрыто запросов: {denied}."
        if still:
            msg += f" Не удалось прервать: {len(still)} — они завершатся сами."
        msg += " Новые действия с последствиями заблокированы, пока вы не скажете «продолжи»."
        return msg

    # ------------------------------------------------------------------ approvals
    def decide(self, aid: str, approve: bool, edited: dict | None = None, remember: bool = False) -> dict:
        a = self.approvals.get(aid)
        if a and approve and edited is not None:
            # Only fields the card offered for editing may change; targets (paths, recipients…) may not.
            allowed = set((a["summary"] or {}).get("editable") or [])
            changed = {k for k in set(edited) | set(a["args"]) if edited.get(k) != a["args"].get(k)}
            if changed - allowed:
                raise ApprovalError("Эти параметры нельзя менять при подтверждении: " + ", ".join(sorted(changed - allowed)))
        a = self.approvals.decide(aid, approve, edited)
        if approve and remember:
            tool = self.gw.registry.get(a["tool_id"])
            if tool:
                self.policy.grant_session(tool.spec)
        if a["task_id"] not in self.rt:
            # The task that asked lost its runtime (JARVIS restarted): resume it here.
            asyncio.create_task(self._resume_from_approval(a))
        self._touch()
        return a

    async def _resume_from_approval(self, a: dict) -> None:
        tid = a["task_id"]
        t = self.tasks.get(tid)
        if not t or S(t["state"]) in TERMINAL:
            return
        cid = t["conversation_id"] or self.conversation_id()
        if a["step_id"]:
            self.tasks.update_step(a["step_id"], state="resumed" if a["status"] == "approved" else "denied")
        if a["status"] != "approved":
            self._safe_transition(tid, S.CANCELLED, error="Вы отклонили действие")
            self.add_message(cid, "assistant", "Хорошо, не выполняю.", tid, meta={"route": "approval"})
            return
        runtime = Runtime()
        self.rt[tid] = runtime

        async def go():
            try:
                self._safe_transition(tid, S.RUNNING)
                out = await self.gw.execute(tid, a["tool_id"], a["decided_args"] or a["args"],
                                            "Продолжение после перезапуска (подтверждено вами)",
                                            conversation=self.ctx(cid), preapproved=a["id"],
                                            gate=self._gate(tid))
                self._finish(tid, cid, self.outcome_text(out), meta={"route": "resumed"},
                             failed=out.status in ("failed", "invalid"))
            except asyncio.CancelledError:
                self._safe_transition(tid, S.CANCELLED, error="Отменено")
            finally:
                self.rt.pop(tid, None)
                self._touch()
        runtime.task = asyncio.create_task(go())

    # ------------------------------------------------------------------ access level
    def set_access(self, level: str, quiet: bool = False) -> str:
        """standard: chosen folders, questions for changes. full (chosen by the user in the window): the whole home
        folder (never keys/passwords/browser profiles), ordinary actions without questions, «да» by voice for any
        confirmation. Irreversible actions (messages to other people, code changes) still ask once."""
        if level not in ("standard", "full"):
            raise ValueError("Неизвестный уровень доступа")
        scopes, work = self._base_scopes
        self.config.read_scopes = list(scopes) + (["~"] if level == "full" else [])
        self.config.code_workspaces = list(work) + (["~"] if level == "full" else [])
        self.policy.full_access = level == "full"
        if not quiet:
            self.db.set_kv("access_level", level)
            self.audit.write("access.changed", level=level)
            self._touch()
        return level

    def voice_approvable(self, tool_id: str) -> bool:
        from .comms.agent import VOICE_APPROVABLE
        return tool_id in VOICE_APPROVABLE or (self.policy.full_access and tool_id != "trust_rule_add")

    def _speak_approval(self, kind: str, ev: dict | None = None) -> None:
        """In a voice task a confirmation is also asked aloud (message sends phrase their own question)."""
        if kind != "approval.created" or not ev or not self.speech:
            return
        a = ev["approval"]
        r = self.rt.get(a["task_id"])
        if not r or not r.voice or a["tool_id"] in ("message_send", "cloud_send"):
            return
        how = "Скажите «да» или «нет»." if self.voice_approvable(a["tool_id"]) else "Подтвердите кнопкой на экране."
        self.speech.speak_now(a["task_id"], f"Нужно ваше решение: {(a['summary'] or {}).get('action', '')[:160]}. {how}",
                              r.turn)

    # ------------------------------------------------------------------ scheduled messages
    async def deliver_job(self, job: dict) -> None:
        """Outbox executor callback: a scheduled message is due. Runs as a visible task through the Gateway."""
        from .comms.base import CHANNEL_TO
        CH = {"telegram": "Telegram", "whatsapp": "WhatsApp", "email": "почта"}
        cid = self.conversation_id()
        who = f"{job['to_name']} {CHANNEL_TO.get(job['channel'], '')}"
        tid = self.tasks.create(f"Отправка по расписанию: {who}", cid, route="comms", level=0)
        self.rt[tid] = Runtime(voice=bool(self.db.get_kv("comms_announce", True)))
        try:
            self.tasks.transition(tid, S.RUNNING, objective="Наступило время отправки")
            out = await self.gw.execute(tid, "message_deliver", {"job_id": job["id"]},
                                        "Наступило время отправки (подтверждено вами при планировании)")
            j = self.hub.outbox.get(job["id"])
            if j["status"] == "sending":      # policy/validation stopped it before any send attempt
                j = self.hub.outbox.update(j["id"], status="failed", last_error=out.error or "Не отправлено")
            if out.status == "ok":
                text, failed = f"Отправлено по расписанию: {job['to_name']}, {CH.get(job['channel'], job['channel'])}.", False
                if out.verification and out.verification.status == "unverified":
                    text += f" Подтверждения от сервиса пока нет: {out.verification.detail}."
            elif j["status"] == "scheduled":
                text, failed = (f"Не удалось отправить сообщение {who}: {j['last_error']} "
                                f"Повторю в {time.strftime('%H:%M', time.localtime(j['due_at']))}."), True
            else:
                text, failed = f"Сообщение {who} не отправлено: {j.get('last_error') or out.error}.", True
            if self.speech and self.rt[tid].voice:
                self.speech.start(tid, None, 400)
            self._finish(tid, cid, text, meta={"route": "comms", "job_id": job["id"], "scheduled_delivery": True},
                         failed=failed)
            try:
                from .tools.base import run_cmd
                await run_cmd(["notify-send", "-a", "JARVIS", "--", "JARVIS", text], timeout=3)
            except Exception:   # noqa: BLE001
                pass
        finally:
            self.rt.pop(tid, None)
            self._touch()

    def on_missed_message(self, job: dict) -> None:
        from .comms.base import CHANNEL_TO
        self.add_message(self.conversation_id(), "assistant",
                         f"⚠ Сообщение {job['to_name']} {CHANNEL_TO.get(job['channel'], '')} не отправлено: "
                         f"{job['last_error']}. Текст: «{job['body'][:200]}». Скажите, если отправить сейчас.",
                         meta={"route": "comms", "job_id": job["id"]})

    # ------------------------------------------------------------------ reminders
    def _on_reminder(self, r: dict, late: bool) -> None:
        cid = self.conversation_id()
        text = (f"⏰ Пропущенное напоминание (было на {fmt_time(r['due_at'])}): {r['text']}" if late
                else f"⏰ Напоминание: {r['text']}")
        self.add_message(cid, "assistant", text, meta={"route": "reminder", "reminder_id": r["id"]})

    # ------------------------------------------------------------------ state
    def privacy_profile(self) -> str:
        return self.db.get_kv("privacy_profile") or self.config.privacy_profile

    def core_state(self) -> dict:
        pending = self.approvals.pending()
        active = self.tasks.active()
        running = [s for t in active for s in t["steps"] if s["state"] in ("running", "verifying")]
        running_steps = bool(running)
        names = {"claude": "Claude", "claude_code": "Claude Code"}
        working = next((names.get((s.get("args") or {}).get("provider"), "Claude Code" if s["tool_id"].startswith("claude")
                                  else None) for s in running
                        if s["tool_id"] in ("agent_generate", "agent_chat", "claude_code_inspect", "claude_code_change")),
                       None)
        if not working and any(r.thinking for r in self.rt.values()):
            working = "Claude"
        thinking = any(r.thinking for r in self.rt.values())
        paused = [t for t in active if t["state"] == S.PAUSED]
        if self.policy.estop:
            state = "STANDBY"
        elif pending:
            state = "WAITING_APPROVAL"
        elif running_steps:
            state = "EXECUTING"
        elif thinking:
            state = "THINKING"
        elif any(t["state"] in (S.RUNNING, S.PLANNING, S.VERIFYING) for t in active):
            state = "PROCESSING"
        elif paused:
            state = "PAUSED"
        elif self._last_finish and time.time() - self._last_finish[0] < COMPLETED_FLASH_S:
            state = "ERROR" if self._last_finish[1] == "error" else "COMPLETED"
        else:
            state = "IDLE"
        return {"state": state, "active_tasks": len(active), "pending_approvals": len(pending), "working": working,
                "estop": self.policy.estop, "ai": self._ai_summary(),
                "privacy": self.privacy_profile(), "access": "full" if self.policy.full_access else "standard"}

    def _ai_summary(self) -> str:
        if not self.providers:
            return "unconfigured"
        hs = {self.providers.health(pid).value for pid in self.providers.providers}
        return "ready" if "CONNECTED" in hs else "partial" if "DEGRADED" in hs else "unconfigured"

    def _touch(self) -> None:
        s = self.core_state()
        if s != self._state:
            self._state = s
            self.bus.publish("core.state", s)

    def snapshot(self) -> dict:
        cid = self.conversation_id()
        msgs = self.messages(cid)
        task_ids = {m["task_id"] for m in msgs if m["task_id"]}
        tasks = {t["id"]: t for t in (self.tasks.get(i) for i in task_ids) if t}
        for t in self.tasks.active():
            tasks[t["id"]] = t
        return {"conversation_id": cid, "messages": msgs, "tasks": tasks,
                "recent_tasks": [{"id": t["id"], "request": t["request"], "state": t["state"],
                                  "created_at": t["created_at"], "route": t["route"]}
                                 for t in self.tasks.recent(12)],
                "approvals": self.approvals.pending(), "core": self.core_state(),
                "provider": self.ai.describe(), "tools": self.gw.registry.describe(),
                "providers": self.providers.describe() if self.providers else [],
                "reminders": self.scheduler.upcoming(), "session_grants": sorted(self.policy.session_grants),
                "uptime_s": round(time.time() - self.started_at)}


STATE_RU = {"created": "создана", "queued": "в очереди", "planning": "планирую", "running": "выполняется",
            "waiting_for_approval": "ждёт подтверждения", "paused": "на паузе", "verifying": "проверяю",
            "completed": "готово", "failed": "ошибка", "cancelled": "отменена", "blocked": "заблокирована",
            "retrying": "повтор", "waiting_for_dependency": "ждёт данных"}

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"]
_DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _ru_date(lt) -> str:
    return f"{_DAYS[lt.tm_wday]}, {lt.tm_mday} {_MONTHS[lt.tm_mon - 1]} {lt.tm_year}"


def _wants_depth(text: str) -> bool:
    t = text.casefold()
    return any(k in t for k in ("подумай как следует", "глубокий анализ", "think hard", "детально проанализируй"))
