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

from ..providers.base import sens_rank
from .decision import Decision, DecisionError, Step, validate

VERBS = (r"напиши|подготовь|составь|сравни|найди|отправь|переведи|сохрани|открой|исправь|почини|запусти|проверь|"
         r"выбери|покажи|сделай|объясни|создай|опубликуй|разошли|поищи|узнай|оцени|выдели|посмотри|прочитай|"
         r"ответь|перепиши|отредактируй|проанализируй|скачай|загрузи|установи|удали|создай|перемести|скопируй|"
         r"переименуй|запиши|придумай|переведи|положи|упакуй|распакуй")
SPLIT = re.compile(rf"(?:,?\s+(?:а\s+)?(?:потом|затем|после этого|после чего)\s+|;\s*|\.\s+(?=\S)|"
                   rf",?\s+и\s+(?=(?:{VERBS})\w*\b)|,\s+(?=(?:{VERBS})\w*\b)|,\s+(?=(?:но\s+|и\s+)?(?:пока\s+)?не\s+\w+))", re.I)

LANG = r"(китайск|английск|немецк|казахск|французск|испанск|итальянск|турецк)\w*"
R = {
    "negated_send": re.compile(r"\bне\s+(?:отправляй|отсылай|публикуй)|пока\s+не\s+отправ"),
    "send": re.compile(r"\b(отправь|отошли|разошли|опубликуй|перешли|отправить)\b"),
    "read_mail": re.compile(r"(покажи|открой|найди|прочитай|проверь)\s+(?:мне\s+)?(?:последн\w+\s+|нов\w+\s+)?"
                            r"(письм\w*|почт\w*)"),
    "estimates": re.compile(r"\b(смет\w*|кп\b|коммерческ\w+ предложени\w*)"),
    "code_obj": re.compile(r"(?:исправь|почини|найди|устрани|проверь|посмотри|разберись)\w*\s+(?:с\s+)?(?:ошибк\w*|баг\w*)(?!.{0,20}\b(?:в|во)\s+"
                           r"(?:письм|текст|сообщени|документ|переводе|смет))|\b(код\w*|баг\w*|тест\w*|скрипт\w*|функци\w*|репозитори\w*|модул\w*|сборк\w*|"
                           r"компиляц\w*|лог\w*|traceback|python|javascript|typescript|rust|программ\w*|"
                           r"приложени\w* не запуска\w*|не запускается|падает|крашится)"),
    "files": re.compile(r"\b(созда\w*|сохрани\w*|запиши|перемести\w*|переименуй\w*|скопируй\w*|удали\w*|скачай\w*|"
                        r"загрузи\w*|распакуй\w*|упакуй\w*|заархивируй\w*|разложи\w*|упорядочи\w*)\b.{0,70}"
                        r"\b(файл\w*|папк\w*|директори\w*|каталог\w*|архив\w*|документы|загрузки|рабочий стол|прайс\w*)"
                        r"|\bсохрани\s+(?:это|результат\w*|их|ответ|всё|все)\b|\bскачай\w*\b"),
    "install": re.compile(r"\b(установи|поставь|доустанови|обнови|удали|подключи)\w*\s+.{0,30}"
                          r"(зависимост\w*|пакет\w*|библиотек\w*|модул\w*|программ\w*|приложени\w*|утилит\w*)"),
    "system": re.compile(r"\b(hyprland|waybar|omarchy|wayland|quickshell|systemd|journalctl|терминал\w*|консол\w*|"
                         r"shell|bash|git\b|коммит\w*|ветк\w* git|репозитор\w*|лог\w*|конфиг\w*)"),
    "code_change": re.compile(r"\b(исправь|почини|поправь|пофикси|измени|добавь|создай|отрефактор\w*|"
                              r"запусти|прогони|удали|переименуй|обнови)\w*"),
    "vision": re.compile(r"(посмотри|глянь|взгляни)\s+на\s+(экран|фото|картинк\w*|изображени\w*)|что\s+(у меня\s+)?на\s+"
                         r"экране|\bна экране\b|скриншот\w*.*(объясни|что)|камер\w*"),
    "research": re.compile(r"в интернете|в сети\b|онлайн|погугли|загугли|найди информаци\w*|поищи информаци\w*|"
                           r"(найди|поищи|подбери)\s+(?:\d+\s+|три\s+|пять\s+|несколько\s+|\w+\s+)?"
                           r"(поставщик\w*|производител\w*|компани\w*|фабрик\w*|магазин\w*|отзыв\w*|аналог\w*|"
                           r"подрядчик\w*|цен\w*)|узнай.*(о компании|про компанию|о фабрике)"),
    "translate": re.compile(r"\b(переведи|перевод\w*)\b"),
    "write": re.compile(r"(напиши|составь|подготовь|набросай|сформулируй|ответь|придумай|сочини)\b.*\b(письм\w*|ответ\w*|"
                        r"сообщени\w*|текст\w*|пост\w*|обращени\w*|претензи\w*|приглашени\w*|поздравлени\w*|вопрос\w*|"
                        r"отчёт\w*|отчет\w*|описани\w*|слоган\w*|рекламн\w*|статью|стих\w*|историю|резюме)|"
                        r"(сделай|подготовь|напиши|составь)\s+.{0,20}(резюме|выжимк\w*|конспект\w*|сводк\w*)|"
                        r"\bпридумай\b|\bвычитай\b|исправь\s+ошибк\w*\s+(?:в|во)\s+(?:письм|текст|сообщени)\w*|"
                        r"сделай\s+(?:это\s+|его\s+|её\s+)?(?:сообщение\s+|письмо\s+|текст\s+)?(?:более\s+)?"
                        r"(официальн|вежлив|коротк|кратк|дружелюбн|понятн)\w*|перепиши|отредактируй|перефразируй|"
                        r"^напиши\b|^ответь\b"),
    "analyze": re.compile(r"\b(сравни|проанализируй|оцени|сопоставь|резюмируй|подведи итог\w*|сделай вывод\w*)\b|"
                          r"выбери\s+\d*\s*\w*\s*(лучш|подходящ)\w*|найди расхождени\w*|выдели главн\w*|расхождени\w*"),
}
OVERRIDE = re.compile(r"(?:^|\s|,)(?:через|используй|с помощью|пусть это сделает|пусть)\s+"
                      r"(claude(?:\s+code)?|клод(?:\s+код)?|клауд(?:\s+код)?)\b[,.:]?|"
                      r"^(claude(?:\s+code)?|клод(?:\s+код)?)\s*[,:]", re.I)
OVERRIDE_MAP = {"claude": "claude", "клод": "claude", "клауд": "claude", "claude code": "claude_code",
                "клод код": "claude_code", "клауд код": "claude_code"}
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
    name = re.sub(r"\s+", " ", (m.group(1) or m.group(2) or "").casefold().replace("ё", "е"))
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
    explicit_web = re.search(r"в интернете|в сети\b|онлайн|погугли|загугли", t)
    if explicit_web and not R["files"].search(t):
        return "research", 0.9
    if R["install"].search(t):
        return "change_code", 0.9
    if R["files"].search(t) and not R["write"].search(t):
        return "files", 0.9
    if R["code_obj"].search(t) or R["system"].search(t):
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
    if prev in ("research", "analyze", "write") and re.search(r"\bсохрани|\bзапиши|\bположи|\bскачай", t):
        return "files", 0.8
    if prev in ("inspect_code", "change_code") and R["code_change"].search(t):
        return "change_code", 0.75
    if prev in ("inspect_code", "change_code"):
        return "inspect_code", 0.6
    if prev in ("research", "compare_estimates", "analyze", "read_mail") and re.search(VERBS, t):
        return "analyze", 0.6
    return "chat", 0.5


REMOVED_PROVIDER = re.compile(r"(?:через|используй|с помощью)?\s*\b(gpt|джипити|чат\s?гпт|chatgpt|openai|gemini|джемини|"
                              r"гемини|джимини)\b[,.:]?", re.I)


def heuristic_plan(task_id: str, text: str) -> tuple[Decision, float]:
    from ..router import strip_wake
    text_in = strip_wake(text)
    removed_note = None
    if REMOVED_PROVIDER.search(text_in):
        removed_note = "Других ИИ (GPT, Gemini) в JARVIS нет — всё выполняет Claude через Claude Code."
        text_in = REMOVED_PROVIDER.sub(" ", text_in).strip(" ,.:") or text_in
    override, body = parse_override(text_in)
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
    # Computer work that follows a vague clause, or several computer actions in a row, is one Claude Code session
    # (one approval): «посмотри проект и исправь ошибку», «создай файл и запусти тесты».
    computer = ("change_code", "inspect_code", "files")
    for i in range(len(clauses) - 1, 0, -1):
        a, b = clauses[i - 1], clauses[i]
        if (a.action == "chat" and b.action in computer) or (a.action in computer and b.action in computer):
            merged = "inspect_code" if a.action in ("chat", "inspect_code") and b.action == "inspect_code" else \
                "files" if {a.action, b.action} <= {"files", "chat"} else "change_code"
            clauses[i - 1:i + 1] = [Clause(a.text + "; " + b.text, merged, min(0.85, b.confidence))]
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
    if any(s.action in ("send_message", "change_code", "files") for s in steps):
        risk = "high"
    elif any(s.action == "vision_screen" for s in steps):
        risk = "moderate"
    d = Decision(task_id=task_id, objective=text.strip(), classification=classify_objective(steps), risk=risk,
                 sensitivity=sensitivity_of(text, steps), mode="", steps=steps, override=override,
                 verification=verifications(steps))
    if constraints:
        d.notes.append("Ограничение пользователя: " + "; ".join(constraints))
    if removed_note:
        d.notes.append(removed_note)
    conf = min(c.confidence for c in clauses)
    if len(clauses) == 1 and clauses[0].action == "chat":
        conf = 0.8                            # a plain question is a confident «chat»
    return d, conf


def classify_objective(steps: list[Step]) -> str:
    acts = {s.action for s in steps} - {"read_file"}
    if acts == {"files"}:
        return "software"
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
    local = {"compare_estimates", "read_mail", "inspect_code", "change_code", "vision_screen", "files", "read_file"}
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
    if "files" in acts:
        v += ["files_changed_listed", "backup_before_change"]
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


# ---------------------------------------------------------------- where Claude Code may work
FOLDER_WORDS = {"документ": "~/Documents", "загрузк": "~/Downloads", "рабочий стол": "~/Desktop", "рабочем столе": "~/Desktop",
                "изображени": "~/Pictures", "картинк": "~/Pictures", "проект": "~/Projects", "музык": "~/Music",
                "видео": "~/Videos"}
SYSTEM_DIRS = {"hyprland": "~/.config/hypr", "hypr": "~/.config/hypr", "waybar": "~/.config/waybar",
               "omarchy": "~/.config/omarchy", "walker": "~/.config/walker", "mako": "~/.config/mako"}


def claude_roots(config) -> list[Path]:
    """Folders Claude Code may work in: projects, the user's own folders, and desktop config folders."""
    raw = [*config.code_workspaces, *config.read_scopes, config.write_dir, *SYSTEM_DIRS.values()]
    out = []
    for r in raw:
        p = Path(os.path.expanduser(r)).resolve()
        if p not in out:
            out.append(p)
    return out


def find_workdir(text: str, workspaces: list[str], last: str | None = None, action: str = "change_code",
                 default: str | None = None) -> str | None:
    roots = [Path(os.path.expanduser(w)).resolve() for w in workspaces]
    within = lambda p: any(p == r or p.is_relative_to(r) for r in roots)   # noqa: E731
    for m in re.finditer(r"(~?/[\w./\-]+)", text):
        p = Path(os.path.expanduser(m.group(1).rstrip("."))).resolve()
        if p.is_file():
            p = p.parent
        if p.is_dir() and within(p):
            return str(p)
    low = text.casefold().replace("ё", "е")
    for word, d in SYSTEM_DIRS.items():
        if re.search(rf"\b{word}", low):
            p = Path(os.path.expanduser(d)).resolve()
            if p.is_dir() and within(p):
                return str(p)
    words = {w.casefold() for w in re.findall(r"[\w\-]{3,}", text)}
    for r in roots:
        if r.is_dir() and r.name.casefold() in words:
            return str(r)
    for r in roots:
        if r.is_dir():
            try:
                children = sorted(c for c in r.iterdir() if c.is_dir() and not c.name.startswith("."))
            except OSError:
                continue
            for child in children:
                if child.name.casefold() in words:
                    return str(child)
    if action == "files":
        for word, d in FOLDER_WORDS.items():
            if re.search(rf"\b(?:в|во|на)\s+(?:папк\w*\s+)?{word}", low):
                p = Path(os.path.expanduser(d)).resolve()
                if p.is_dir() and within(p):
                    return str(p)
        if default:
            Path(default).mkdir(parents=True, exist_ok=True)
            return default
    if last and Path(last).is_dir() and within(Path(last)):
        return last
    return None
