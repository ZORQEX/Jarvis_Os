"""Provider selection for each step.

Hard constraints first (capability, enabled, health, privacy profile, data
sensitivity, fallback policy, explicit override); only then a score ranks the
remaining candidates. Nothing is substituted silently: every fallback and every
blocked step carries a human-readable reason in the decision."""
from __future__ import annotations

from ..providers.base import CAP_RU, Cap, sens_rank
from ..providers.registry import ProviderManager
from .decision import Decision, Step
from .planner import CONF, HIGH_SENS

# Role assignment: primary specialist first, then allowed alternates.
PREFS: dict[str, list[str]] = {
    # Claude Code: computer & engineering. Gemini: everything else that needs AI.
    Cap.CODE: ["claude_code"], Cap.TERMINAL: ["claude_code"], Cap.FILESYSTEM: ["claude_code"],
    Cap.WEB_RESEARCH: ["gemini"], Cap.VISION: ["gemini"], Cap.VOICE: ["gemini"], Cap.WRITING: ["gemini"],
    Cap.TRANSLATION: ["gemini"], Cap.DOC_ANALYSIS: ["gemini"], Cap.GENERAL: ["gemini"],
    Cap.DESKTOP_TOOLS: ["gemini"], Cap.PLANNING: ["gemini"],
}
TOOL_FOR = {"compare_estimates": "flora_compare", "read_mail": "mail_read", "send_message": "email_send",
            "read_file": "read_file"}
TOOL_MISSING = {"mail_read": "Почта (Gmail) ещё не подключена — читать письма пока нельзя. Демо-ящик есть в проекте «Флора».",
                "email_send": "Сообщения и письма отправляет модуль связи — скажите, например: «Отправь Ивану в Telegram: "
                              "текст» или «Напиши Сергею на почту, что документы готовы». Перед отправкой я переспрошу.",
                "flora_compare": "Модуль смет недоступен."}
LOCAL_DATA = {"compare_estimates", "read_mail", "inspect_code", "change_code", "vision_screen", "read_file", "files"}


def step_sensitivity(step: Step, by_id: dict[str, Step], sens: dict[str, str]) -> str:
    t = step.instruction.casefold()
    own = "HIGHLY_SENSITIVE" if HIGH_SENS.search(t) else "CONFIDENTIAL" if (CONF.search(t) or step.action in LOCAL_DATA) \
        else "PUBLIC" if step.action == "research" else "INTERNAL"
    for d in step.depends_on:
        if sens_rank(sens.get(d, "INTERNAL")) > sens_rank(own):
            own = sens[d]
    return own


def score(mgr: ProviderManager, pid: str, rank: int) -> tuple[float, dict]:
    fr = mgr.failure_rate(pid)
    p50 = mgr.latency_p50(pid)
    lat = 0.5 if p50 is None else max(0.0, min(1.0, 1 - (p50 - 2000) / 8000))
    parts = {"role": 1.0 if rank == 0 else 0.7, "reliability": 1 - fr, "latency": round(lat, 2), "cost": 0.5}
    total = 0.5 * parts["role"] + 0.3 * parts["reliability"] + 0.1 * parts["latency"] + 0.1 * parts["cost"]
    return round(total, 3), parts


def choose(mgr: ProviderManager, cap: Cap, sensitivity: str, privacy: str, override: str | None = None,
           exclude: set[str] | None = None) -> tuple[str | None, str, list[dict]]:
    """Return (provider_id | None, reason, candidates)."""
    exclude = exclude or set()
    prefs = PREFS.get(cap, [])
    cands = []
    if override:
        ok, why = mgr.eligible(override, cap, sensitivity, privacy)
        cands.append({"provider": override, "eligible": ok, "why": why, "score": None})
        if ok and override not in exclude:
            return override, f"вы выбрали {mgr.get(override).display_name}", cands
        name = mgr.get(override).display_name if mgr.get(override) else override
        return None, f"вы выбрали {name}, но {why if not ok else 'он уже не справился с этим шагом'} — " \
                     "другого исполнителя без вашего согласия не подставляю", cands
    best, best_score, primary_problem = None, -1.0, None
    for rank, pid in enumerate(prefs):
        ok, why = mgr.eligible(pid, cap, sensitivity, privacy)
        if ok and pid in exclude:
            ok, why = False, "уже не справился с этим шагом"
        if ok and rank > 0 and not mgr.settings(pid)["fallback_eligible"]:
            ok, why = False, "запрещён как резерв в настройках"
        s, parts = score(mgr, pid, rank) if ok else (None, {})
        cands.append({"provider": pid, "eligible": ok, "why": why, "score": s, "parts": parts})
        if rank == 0 and not ok:
            primary_problem = f"{mgr.get(pid).display_name if mgr.get(pid) else pid}: {why}"
        if ok and s > best_score:
            best, best_score = pid, s
    if best is None:
        why = "; ".join(f"{c['provider']}: {c['why']}" for c in cands) or "нет исполнителя"
        return None, f"нет доступного исполнителя для «{CAP_RU.get(cap, cap)}» ({why})", cands
    p = mgr.get(best)
    if primary_problem and best != prefs[0]:
        return best, f"{primary_problem} → {p.display_name} по политике резерва", cands
    return best, f"{p.display_name}: {CAP_RU.get(cap, cap)} — основная роль", cands


def route(d: Decision, mgr: ProviderManager, tools: set[str], privacy: str) -> Decision:
    by_id = {s.id: s for s in d.steps}
    sens: dict[str, str] = {}
    override = d.override
    if override and override in mgr.providers:
        supported = [s for s in d.steps if s.capability and Cap(s.capability) in mgr.providers[override].capabilities]
        if not supported:
            name = mgr.providers[override].display_name
            d.notes.append(f"{name} не выполняет такие задачи ({', '.join(CAP_RU.get(Cap(s.capability), s.capability) for s in d.steps if s.capability) or 'действия без ИИ'}).")
    for s in d.steps:
        sens[s.id] = step_sensitivity(s, by_id, sens)
        if s.executor_kind == "tool":
            tool = TOOL_FOR[s.action]
            s.executor = tool
            if tool not in tools:
                s.status, s.reason = "blocked", TOOL_MISSING.get(tool, "инструмент недоступен")
            else:
                s.reason = "детерминированный инструмент — ИИ не нужен"
            if s.action == "send_message":
                s.approval = "required"
            continue
        cap = Cap(s.capability)
        if s.action == "chat":
            cap = Cap.GENERAL
        use_override = override if override and override in mgr.providers and \
            cap in mgr.providers[override].capabilities else None
        if override and not use_override and override in mgr.providers:
            s.reason = f"{mgr.providers[override].display_name} не умеет «{CAP_RU.get(cap, cap)}» — " \
                       f"исполнителя выбираю сам"
        pid, why, cands = choose(mgr, cap, sens[s.id], privacy, use_override)
        s.candidates = cands
        s.executor = pid
        s.reason = (s.reason + "; " if s.reason else "") + why
        if pid is None:
            s.status = "blocked"
        if s.action in ("change_code", "files"):
            s.approval = "required"
    # Steps that depend on blocked steps cannot run.
    for s in d.steps:
        if s.status == "planned" and any(by_id[x].status == "blocked" for x in s.depends_on):
            s.status, s.reason = "blocked", "зависит от шага, который нельзя выполнить"
    executors = {s.executor for s in d.steps if s.executor_kind == "provider" and s.executor}
    has_tool = any(s.executor_kind == "tool" for s in d.steps)
    planned_ai = any(s.executor_kind == "provider" for s in d.steps)
    d.mode = "blocked" if planned_ai and not executors else "tools" if not executors else ("ai_tools" if has_tool else "multi_ai" if len(executors) > 1 else "single_ai")
    d.step_sensitivity = sens
    return d
