"""Planner: turns one objective into ordered steps with capabilities.

1. Deterministic planner — splits the whole request into action clauses and
   classifies each clause by what it asks for (action + object), carrying the
   domain across clauses («проверь, почему не запускается, и исправь»).
2. LLM planner — used only for compound requests the deterministic planner is
   unsure about, and only if a planning-capable provider is available. Its
   output is schema-validated; risk/sensitivity can only go up, never down.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..providers.base import SENSITIVITY, sens_rank
from .decision import ACTIONS, Decision, DecisionError, Step, validate

VERBS = (r"напиши|подготовь|составь|сравни|найди|отправь|переведи|сохрани|открой|исправь|почини|запусти|проверь|"
         r"выбери|покажи|сделай|объясни|создай|опубликуй|разошли|поищи|узнай|оцени|выдели|посмотри|прочитай|"
         r"ответь|перепиши|отредактируй|проанализируй")
SPLIT = re.compile(rf"(?:,?\s+(?:а\s+)?(?:потом|затем|после этого|после чего)\s+|;\s*|\.\s+(?=\S)|"
                   rf",?\s+и\s+(?=(?:{VERBS})\w*\b)|,\s+(?=(?:{VERBS})\w*\b)|,\s+(?=(?:но\s+|и\s+)?(?:пока\s+)?не\s+\w+))", re.I)

LANG = r"(китайск|английск|немецк|казахск|французск|испанск|итальянск|турецк)\w*"
R = {
    "negated_send": re.compile(r"\bне\s+(?:отправляй|отсылай|публикуй)|пока\s+не\s+отправ"),
    "send": re.compile(r"\b(отправь|отошли|разошли|опубликуй|перешли|отправить)\b"),
    "read_mail": re.compile(r"(покажи|открой|найди|прочитай|проверь)\s+(?:мне\s+)?(?:последн\w+\s+|нов\w+\s+)?"
                            r"(письм\w*|почт\w*)"),
    "estimates": re.compile(r"\b(смет\w*|кп\b|коммерческ\w+ предложени\w*)"),
    "code_obj": re.compile(r"\b(код\w*|баг\w*|тест\w*|скрипт\w*|функци\w*|репозитори\w*|модул\w*|сборк\w*|"
                           r"компиляц\w*|лог\w*|traceback|python|javascript|typescript|rust|программ\w*|"
                           r"приложени\w* не запуска\w*|не запускается|падает|крашится)"),
    "code_change": re.compile(r"\b(исправь|почини|поправь|пофикси|измени|добавь|создай|отрефактор\w*|"
                              r"запусти|прогони|удали|переименуй|обнови)\w*"),
    "vision": re.compile(r"(посмотри|глянь|взгляни)\s+на\s+(экран|фото|картинк\w*|изображени\w*)|что\s+(у меня\s+)?на\s+"
                         r"экране|\bна экране\b|скриншот\w*.*(объясни|что)|камер\w*"),
    "research": re.compile(r"в интернете|в сети\b|онлайн|погугли|загугли|найди информаци\w*|поищи информаци\w*|"
                           r"(найди|поищи|подбери)\s+(?:\d+\s+|три\s+|пять\s+|несколько\s+|\w+\s+)?"
                           r"(поставщик\w*|производител\w*|компани\w*|фабрик\w*|магазин\w*|отзыв\w*|аналог\w*|"
                           r"подрядчик\w*|цен\w*)|узнай.*(о компании|про компанию|о фабрике)"),
    "translate": re.compile(r"\b(переведи|перевод\w*)\b"),
    "write": re.compile(r"(напиши|составь|подготовь|набросай|сформулируй|ответь)\b.*\b(письм\w*|ответ\w*|сообщени\w*|"
                        r"текст\w*|пост\w*|обращени\w*|претензи\w*|приглашени\w*|поздравлени\w*|вопрос\w*)|"
                        r"сделай\s+(?:это\s+|его\s+|её\s+)?(?:сообщение\s+|письмо\s+|текст\s+)?(?:более\s+)?"
                        r"(официальн|вежлив|коротк|кратк|дружелюбн|понятн)\w*|перепиши|отредактируй|перефразируй|"
                        r"^напиши\b|^ответь\b"),
    "analyze": re.compile(r"\b(сравни|проанализируй|оцени|сопоставь|резюмируй|подведи итог\w*|сделай вывод\w*)\b|"
                          r"выбери\s+\d*\s*\w*\s*(лучш|подходящ)\w*|найди расхождени\w*|выдели главн\w*|расхождени\w*"),
}
OVERRIDE = re.compile(r"(?:^|\s|,)(?:через|используй|с помощью|пусть это сделает|пусть)\s+"
                      r"(gpt|джипити|чат\s?гпт|chatgpt|openai|gemini|джемини|гемини|claude|клод|клауд)\b[,.]?|"
                      r"^(claude|клод|gpt|джипити|gemini|джемини)\s*,", re.I)
OVERRIDE_MAP = {"gpt": "openai", "джипити": "openai", "чатгпт": "openai", "чат гпт": "openai", "chatgpt": "openai",
                "openai": "openai", "gemini": "gemini", "джемини": "gemini", "гемини": "gemini",
                "claude": "claude_code", "клод": "claude_code", "клауд": "claude_code"}
HIGH_SENS = re.compile(r"\bпарол\w*|\bпаспорт\w*|\bномер\w* (?:банковск\w+ )?карт\w*|\bcvv|\bcvc|\bпин-?код\w*|"
                       r"\bснилс|\bseed[- ]фраз\w*|\bприватн\w* ключ\w*|private key|\bpassword", re.I)
CONF = re.compile(r"флор\w*|смет\w*|договор\w*|письм\w* от|счёт|счет\b|кп\b|бюджет\w*|зарплат\w*")


@dataclass
class Clause:
    text: str
    action: str
    confidence: float


def parse_override(text: str) -> tuple[str | None, str]:
    m = OVERRIDE.search(text)
    if not m:
        return None, text
    name = (m.group(1) or m.group(2) or "").casefold().replace("ё", "е")
    pid = OVERRIDE_MAP.get(name)
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip(" ,.")
    return pid, cleaned or text


def split_clauses(text: str) -> list[str]:
    parts = [p.strip(" ,.;") for p in SPLIT.split(text) if p and p.strip(" ,.;")]
    return parts or [text.strip()]


def classify_clause(c: str, prev: str | None) -> tuple[str, float]:
    t = c.casefold().replace("ё", "е")
    if R["negated_send"].search(t):
        return "", 1.0                      # «не отправляй» — a constraint, not an action
    if R["send"].search(t) and not R["write"].search(t):
        return "send_message", 0.9
    if R["vision"].search(t):
        return "vision_screen", 0.9
    if R["read_mail"].search(t) and not R["research"].search(t):
        return "read_mail", 0.9
    if R["code_obj"].search(t):
        return ("change_code" if R["code_change"].search(t) else "inspect_code"), 0.9
    if R["estimates"].search(t) and re.search(r"сравни|проверь|расхожд|разниц|сопостав", t):
        return "compare_estimates", 0.9
    if R["research"].search(t):
        return "research", 0.9
    if R["translate"].search(t) and not R["write"].search(t):
        return "translate", 0.9
    if R["write"].search(t):
        return "write", 0.9
    if R["analyze"].search(t):
        return "analyze", 0.85
    if prev == "vision_screen" and re.search(r"объясни|что это|почему|подскажи|как исправить", t):
        return "vision_screen", 0.85
    # Verb-only clause continues the previous clause's domain: «…не запускается, и исправь».
    if prev in ("inspect_code", "change_code") and R["code_change"].search(t):
        return "change_code", 0.75
    if prev in ("inspect_code", "change_code"):
        return "inspect_code", 0.6
    if prev in ("research", "compare_estimates", "analyze", "read_mail") and re.search(VERBS, t):
        return "analyze", 0.6
    return "chat", 0.5


def heuristic_plan(task_id: str, text: str) -> tuple[Decision, float]:
    from ..router import strip_wake
    override, body = parse_override(strip_wake(text))
    clauses: list[Clause] = []
    prev = None
    constraints = []
    for c in split_clauses(body):
        action, conf = classify_clause(c, prev)
        if not action:
            constraints.append(c)
            continue
        # merge consecutive identical actions into one step
        if clauses and clauses[-1].action == action and action not in ("send_message",):
            clauses[-1] = Clause(clauses[-1].text + "; " + c, action, min(clauses[-1].confidence, conf))
        else:
            clauses.append(Clause(c, action, conf))
        prev = action
    if not clauses:
        clauses = [Clause(body, "chat", 0.5)]
    # A vague first clause takes the domain of the next one: «создай файл и запусти тесты» is one code task.
    for i in range(len(clauses) - 1, 0, -1):
        a, b = clauses[i - 1], clauses[i]
        if a.action == "chat" and b.action in ("change_code", "inspect_code") and R["code_change"].search(a.text.casefold()):
            clauses[i - 1:i + 1] = [Clause(a.text + "; " + b.text, "change_code", 0.8)]
    # «найди … и сравни» → research then analyze; «проверь сметы и найди расхождения» → tool then analyze.
    steps: list[Step] = []
    for i, cl in enumerate(clauses, 1):
        deps = [s.id for s in steps if s.action != "send_message"]
        if cl.action == "research" and steps and all(s.action == "research" for s in steps):
            deps = []                         # independent research steps may run in parallel
        steps.append(Step(id=f"s{i}", action=cl.action, instruction=cl.text, depends_on=deps))
    if constraints:
        for s in steps:
            s.instruction += " (" + "; ".join(constraints) + ")"
    risk = "low"
    if any(s.action in ("send_message", "change_code") for s in steps):
        risk = "high"
    elif any(s.action == "vision_screen" for s in steps):
        risk = "moderate"
    d = Decision(task_id=task_id, objective=text.strip(), classification=classify_objective(steps), risk=risk,
                 sensitivity=sensitivity_of(text, steps), mode="", steps=steps, override=override,
                 verification=verifications(steps))
    if constraints:
        d.notes.append("Ограничение пользователя: " + "; ".join(constraints))
    conf = min(c.confidence for c in clauses)
    if len(clauses) == 1 and clauses[0].action == "chat":
        conf = 0.8                            # a plain question is a confident «chat»
    return d, conf


def classify_objective(steps: list[Step]) -> str:
    acts = {s.action for s in steps}
    if acts & {"inspect_code", "change_code"} and not acts - {"inspect_code", "change_code", "vision_screen"}:
        return "software" if "vision_screen" not in acts else "visual_diagnosis"
    if "vision_screen" in acts:
        return "visual_diagnosis"
    if acts & {"write", "send_message", "translate"} and len(acts) > 1:
        return "business_communication" if acts & {"research", "compare_estimates", "read_mail", "analyze"} else "writing"
    if acts == {"write"} or acts == {"translate"}:
        return "writing"
    if acts == {"research"} or acts == {"research", "analyze"}:
        return "research"
    if acts & {"compare_estimates", "analyze"}:
        return "document_analysis"
    if acts == {"read_mail"}:
        return "mail"
    if acts == {"chat"}:
        return "conversation"
    return "mixed"


def sensitivity_of(text: str, steps: list[Step]) -> str:
    t = text.casefold()
    if HIGH_SENS.search(t):
        return "HIGHLY_SENSITIVE"
    local = {"compare_estimates", "read_mail", "inspect_code", "change_code", "vision_screen"}
    if CONF.search(t) or any(s.action in local for s in steps):
        return "CONFIDENTIAL"
    if all(s.action == "research" for s in steps):
        return "PUBLIC"
    return "INTERNAL"


def verifications(steps: list[Step]) -> list[str]:
    v = []
    acts = {s.action for s in steps}
    if "research" in acts:
        v.append("sources_present")
    if acts & {"write", "translate"}:
        v += ["numbers_grounded", "draft_presented_not_sent"]
    if "change_code" in acts:
        v += ["files_changed_listed", "tests_rerun_independently"]
    if "send_message" in acts:
        v += ["explicit_approval", "provider_acknowledgment"]
    return v


# ---------------------------------------------------------------- LLM planner
PLANNER_PROMPT = """Разбей задачу пользователя на шаги. Верни ТОЛЬКО JSON вида
{"steps":[{"id":"s1","action":"<action>","instruction":"<что сделать в этом шаге, по-русски>","depends_on":[]}]}
Допустимые action: research (поиск в интернете), analyze (сравнение/анализ данных), write (письмо/текст),
translate (перевод), vision_screen (посмотреть на экран), chat (ответ/беседа/действия на компьютере),
inspect_code (посмотреть код/логи), change_code (изменить код/файлы проекта, запустить тесты),
compare_estimates (сравнить сметы проекта), read_mail (показать письма), send_message (отправить сообщение).
Не больше 6 шагов. Не добавляй send_message, если пользователь не просил отправить. Задача:\n"""


async def llm_plan(provider, task_id: str, text: str, base: Decision, known_providers, known_tools) -> Decision | None:
    from ..providers.base import Cap, StepRequest
    try:
        r = await provider.generate(StepRequest(Cap.PLANNING, PLANNER_PROMPT + text, max_tokens=800,
                                                system="Ты планировщик задач. Отвечай только JSON."))
        raw = r.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        steps = [Step(id=str(s["id"]), action=str(s["action"]), instruction=str(s["instruction"])[:2000],
                      depends_on=[str(x) for x in s.get("depends_on", [])]) for s in data["steps"]][:6]
        d = Decision(task_id=task_id, objective=text, classification=classify_objective(steps),
                     risk=base.risk, sensitivity=base.sensitivity, mode="", steps=steps,
                     planner=f"llm:{provider.id}", override=base.override, verification=verifications(steps),
                     notes=list(base.notes))
        validate(d, known_providers, known_tools)
        # Policy fields only move towards safety.
        heur = {"low": 0, "moderate": 1, "high": 2}
        if heur[base.risk] > heur[d.risk]:
            d.risk = base.risk
        if sens_rank(base.sensitivity) > sens_rank(d.sensitivity):
            d.sensitivity = base.sensitivity
        d.sensitivity = max(d.sensitivity, sensitivity_of(text, d.steps), key=sens_rank)
        # A model may not add a send the user did not ask for.
        if any(s.action == "send_message" for s in d.steps) and not any(s.action == "send_message" for s in base.steps):
            d.steps = [s for s in d.steps if s.action != "send_message"]
            d.notes.append("Планировщик предлагал отправку — удалено: вы её не просили")
        return d
    except (ValueError, KeyError, TypeError, DecisionError):
        return None
    except Exception:   # noqa: BLE001 — provider failure → keep deterministic plan
        return None


# ---------------------------------------------------------------- workdir for code steps
def find_workdir(text: str, workspaces: list[str], last: str | None = None) -> str | None:
    roots = [Path(os.path.expanduser(w)).resolve() for w in workspaces]
    for m in re.finditer(r"(~?/[\w./\-]+)", text):
        p = Path(os.path.expanduser(m.group(1))).resolve()
        if p.is_dir() and any(p == r or p.is_relative_to(r) for r in roots):
            return str(p)
    words = {w.casefold() for w in re.findall(r"[\w\-]{3,}", text)}
    for r in roots:
        if r.is_dir() and r.name.casefold() in words:
            return str(r)
    for r in roots:
        if r.is_dir():
            for child in sorted(r.iterdir()):
                if child.is_dir() and child.name.casefold() in words:
                    return str(child)
    if last and Path(last).is_dir():
        return last
    return None
