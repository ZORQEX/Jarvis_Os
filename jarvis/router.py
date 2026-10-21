"""Level-0/1 router: unambiguous commands are mapped straight to tools,
without any model call. Anything not matched confidently goes to Gemini."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools.desktop import APP_ALIASES


@dataclass
class Route:
    kind: str                     # tool | reply | control | search_open | flora
    tool: str | None = None
    args: dict = field(default_factory=dict)
    reply: str | None = None
    why: str = ""
    level: int = 0


# Spellings the local recogniser produces for «Джарвис» (measured: «Джарис», «Джас», «Давис», «Джавис»).
WAKE = re.compile(r"^\s*(?:hey|эй|хэй|хей|ok|окей)?\s*(?:джарвис|jarvis|жарвис|джервис|джарис|джавис|джаревис|"
                  r"жарис|джас|давис|дарвис)(?=[\s,!.:]|$)[\s,!.:]*", re.I)

NUM_WORDS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "одной": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "полчаса": 30,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
}
ORDINALS = {
    "первый": 1, "первую": 1, "первое": 1, "второй": 2, "вторую": 2, "второе": 2, "третий": 3, "третью": 3,
    "третье": 3, "четвертый": 4, "четвертую": 4, "пятый": 5, "пятую": 5, "шестой": 6, "седьмой": 7,
    "восьмой": 8, "девятый": 9, "десятый": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

FOLDERS = {
    "документы": "~/Documents", "загрузки": "~/Downloads", "проекты": "~/Projects", "изображения": "~/Pictures",
    "картинки": "~/Pictures", "фото": "~/Pictures", "музыку": "~/Music", "видео": "~/Videos",
    "заметки": "~/Documents/Jarvis/Заметки", "скриншоты": "~/Pictures/Jarvis", "documents": "~/Documents",
    "downloads": "~/Downloads",
}


def norm(text: str) -> str:
    t = WAKE.sub("", text.strip())
    t = t.casefold().replace("ё", "е")
    t = re.sub(r"[«»\"“”!?.,;]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_number(tok: str) -> int | None:
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    parts = tok.split()
    if parts and all(p in NUM_WORDS for p in parts):
        return sum(NUM_WORDS[p] for p in parts)
    return None


def _ordinal(tok: str) -> int | None:
    if tok in ORDINALS:
        return ORDINALS[tok]
    m = re.fullmatch(r"(\d+)(?:-?(?:й|ю|е|ой|ый|ий))?", tok)
    return int(m.group(1)) if m else None


COMMS_CHANNEL = (r"(телеграм\w*|телеграмм\w*|телег[аеиу]|\bтг\b|telegram|ват[сц]ап\w*|вотсап\w*|вацап\w*|whats ?app|"
                 r"на почту|по почте|почтой|на (e-?mail|имейл|емейл|мейл)|электронн\w+ почт\w*)")
COMMS_VERB = r"\b(отправ\w*|напиши|написать|напишите|пошли|послать|перешли|сообщи|скинь|черкни|запланируй)\b"


def is_comms(t: str) -> bool:
    """Messages to people (send / schedule / status / cancel / edit / trust). Pure text drafting stays elsewhere."""
    if re.search(COMMS_VERB, t) and re.search(COMMS_CHANNEL, t):
        return True
    if re.search(r"\b(отправь|пошли|отправить|запланируй)\b.*\bсообщени", t):
        return True
    if re.search(r"\b(напиши|отправь|пошли)\s+\w+(у|ю|е|и)\s*[:,—-]", t):     # «напиши Ивану: …»
        return True
    if re.search(r"сообщени\w*.*(запланир|отложен)|(запланир|отложен)\w*.*сообщени|"
                 r"что (ты )?(сегодня )?отправил|что не (удалось|получилось) отправить|какие (письма|сообщения)", t):
        return True
    if re.search(r"\b(отмени|измени|поменяй|перенеси)\b.*\b(сообщени|письм|отправк)", t):
        return True
    if re.search(r"подтверждени\w* (больше )?не (спрашивай|нужно|надо)|без подтверждения отправляй", t):
        return True
    if re.search(r"\bзапомни\b.*\b(почт|адрес|номер|телефон|e-?mail|имейл|телеграм)", t):
        return True
    if re.search(COMMS_VERB + r".*\bконтакт", t):            # «напиши любимая, контакту …»
        return True
    return False


def is_comms_followup(t: str, ctx: dict) -> bool:
    """«Отправь ей ещё, что я её люблю» shortly after a message to someone: the same person, not a chat question."""
    import time as _t
    recent = ctx.get("last_contact") and _t.time() - (ctx.get("last_contact_at") or 0) < 30 * 60
    return bool(recent and re.search(r"\b(отправь|напиши|скажи|передай|черкни|пошли|ответь)\b", t)
                and re.search(r"\b(ей|ему|им|ей же|ему же|ещё|еще|тоже|туда же)\b", t))


def strip_wake(text: str) -> str:
    return WAKE.sub("", text.strip())


def route(text: str, ctx: dict | None = None) -> Route | None:
    ctx = ctx or {}
    raw = strip_wake(text)
    t = norm(text)
    if not t:
        # "Джарвис." alone → "Да?"
        return Route("answer", reply="Да?", why="Обращение по имени") if WAKE.match(text) else None

    # --- control -----------------------------------------------------------
    if re.fullmatch(r"(экстренная остановка|остановить все|останови все|прекрати все|emergency stop|стоп все|"
                    r"стоп стоп|отмени все)", t):
        return Route("control", args={"op": "estop"}, why="Экстренная остановка")
    # Conversational stop: stop speaking and drop the current answer; nothing else is blocked.
    if re.fullmatch(r"(стоп|stop|остановись|подожди|погоди|секунду|замолчи|хватит|хватит говорить|помолчи|"
                    r"тише говори|stop talking|shut up|не говори|достаточно|всё хватит|все хватит)", t):
        return Route("control", args={"op": "interrupt"}, why="Перебить ответ")
    if re.fullmatch(r"(отмени|отмена|отменить|отмени задачу|отмени это|cancel|cancel task)", t):
        return Route("control", args={"op": "cancel_last"}, why="Отмена задачи")
    if re.fullmatch(r"(пауза|поставь на паузу|приостанови|pause)", t):
        return Route("control", args={"op": "pause_all"}, why="Пауза")
    if re.fullmatch(r"(продолжи|продолжай|возобнови|resume|continue|сними блокировку|можно продолжать)", t):
        return Route("control", args={"op": "resume_all"}, why="Продолжить")
    if re.fullmatch(r"(новый разговор|начни заново|новая беседа|new chat|new conversation)", t):
        return Route("control", args={"op": "new_conversation"}, why="Новый разговор")

    # --- communication: messages, mail, scheduled sends ----------------------------
    if is_comms(t) or is_comms_followup(t, ctx):
        return Route("comms", args={"text": raw}, why="Сообщения: Telegram, WhatsApp, почта", level=1)

    # --- deterministic calculations (no AI) -------------------------------------
    from .calc import answer as calc_answer
    calc = calc_answer(raw)
    if calc:
        return Route("answer", reply=calc, why="Локальный расчёт")

    # --- simple replies --------------------------------------------------------
    if re.fullmatch(r"(привет|здравствуй|здравствуйте|добрый (день|вечер)|доброе утро|hi|hello|hey)( джарвис)?", t):
        return Route("reply", reply="greeting", why="Приветствие")
    if re.fullmatch(r"(что ты умеешь|помощь|help|какие команды|что ты можешь)", t):
        return Route("reply", reply="help", why="Справка")
    if re.fullmatch(r"(который час|сколько времени|какое (сегодня )?число|какой сегодня день|what time is it|"
                    r"какая сегодня дата|время)", t):
        return Route("reply", reply="time", why="Время и дата")
    if re.search(r"(статус|состояние) задач|что ты (сейчас )?делаешь|какие задачи|task status", t):
        return Route("reply", reply="tasks", why="Статус задач")

    # --- system ----------------------------------------------------------------
    if re.search(r"(состояние|статус|как (там )?(дела у )?) ?(системы|компьютера|ноутбука)|"
                 r"сколько (свободного )?(места|памяти)|заряд|батаре|температур[аы] (процессора|cpu)|"
                 r"(нагрузк[аи]|загрузк[аи]) (процессора|cpu)|что с (памятью|диском|батареей|процессором)|"
                 r"system (status|info)|battery|disk space|how much (memory|space)", t):
        return Route("tool", "system_info", {}, why="Чтение /proc и /sys")

    # --- windows / workspaces --------------------------------------------------
    if re.search(r"(какие|список|покажи) (окна|окон|открытые окна)|какие окна открыты|list windows", t):
        return Route("tool", "window_list", {}, why="Hyprland IPC")
    m = re.fullmatch(r"(?:на|рабочий стол) ([\w-]+)(?: (?:рабочий стол|стол))", t)
    if m and (n := _ordinal(m.group(1)) or parse_number(m.group(1))) and 1 <= n <= 10:
        return Route("tool", "workspace_switch", {"number": n}, why="Hyprland IPC")
    m = re.fullmatch(r"(?:переключись|перейди|переключи|switch|go)(?: на| to)?(?: (?:рабочий стол|workspace|стол))?"
                     r" ([\w-]+)(?: (?:рабочий стол|стол|workspace))?", t) \
        or re.fullmatch(r"(?:рабочий стол|workspace) ([\w-]+)", t)
    if m:
        n = _ordinal(m.group(1)) or parse_number(m.group(1))
        if n and 1 <= n <= 10:
            return Route("tool", "workspace_switch", {"number": n}, why="Hyprland IPC")

    # --- volume ------------------------------------------------------------------
    m = re.search(r"(?:громкость|звук|volume)(?: на| to)? (\d{1,3}|[а-я ]+?)(?: процент\w*|%)?$", t)
    if m and (n := parse_number(m.group(1))) is not None and 0 <= n <= 100:
        return Route("tool", "volume_set", {"level": n}, why="PipeWire (wpctl)")
    if re.search(r"(звук|громкость) на (максимум|полную)", t):
        return Route("tool", "volume_set", {"level": 100}, why="PipeWire (wpctl)")
    if re.search(r"(звук|громкость) на минимум", t):
        return Route("tool", "volume_set", {"level": 10}, why="PipeWire (wpctl)")
    if re.search(r"убав\w* (звук|громкость)", t):
        return Route("tool", "volume_set", {"delta": -10}, why="PipeWire (wpctl)")
    if re.search(r"прибав\w* (звук|громкость)", t):
        return Route("tool", "volume_set", {"delta": 10}, why="PipeWire (wpctl)")
    if re.search(r"(сделай |чуть |немного )*(тише|потише|quieter|volume down)", t):
        return Route("tool", "volume_set", {"delta": -10}, why="PipeWire (wpctl)")
    if re.search(r"(сделай |чуть |немного )*(громче|погромче|louder|volume up)", t):
        return Route("tool", "volume_set", {"delta": 10}, why="PipeWire (wpctl)")
    if re.fullmatch(r"(выключи|отключи) звук|mute|без звука", t):
        return Route("tool", "volume_set", {"mute": True}, why="PipeWire (wpctl)")
    if re.fullmatch(r"(включи|верни) звук|unmute", t):
        return Route("tool", "volume_set", {"mute": False}, why="PipeWire (wpctl)")

    # --- screenshot --------------------------------------------------------------
    if re.search(r"(сделай |сними )?(скриншот|снимок экрана|скрин экрана)|заскринь|take (a )?screenshot|^screenshot$", t):
        return Route("tool", "screenshot", {}, why="grim (Wayland)")

    # --- notes ---------------------------------------------------------------------
    m = re.match(r"\s*(?:запиши|создай|сделай|добавь|сохрани)\s+(?:мне\s+)?заметку\s*[:,—-]?\s*(.+)$", raw, re.I | re.S) \
        or re.match(r"\s*(?:заметка|запиши|запомни|note|take a note)\s*[:,—-]\s*(.+)$", raw, re.I | re.S) \
        or re.match(r"\s*запомни,?\s+что\s+(.+)$", raw, re.I | re.S)
    if m and m.group(1).strip():
        return Route("tool", "create_note", {"text": m.group(1).strip()}, why="Запись файла в Документы/Jarvis")

    # --- reminders -----------------------------------------------------------------
    m = re.match(r"\s*напомни(?:\s+мне)?\s+через\s+(.+?)\s+(секунд\w*|минут\w*|час\w*)\s*(.*)$", raw, re.I | re.S)
    if m:
        amount = parse_number(norm(m.group(1))) if m.group(1).strip().casefold() != "полчаса" else 30
        unit = m.group(2).casefold()
        if amount is None and unit.startswith(("минут", "час")) and norm(m.group(1)) in ("", "одну"):
            amount = 1
        if amount:
            minutes = amount / 60 if unit.startswith("секунд") else amount * 60 if unit.startswith("час") else amount
            what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(3).strip(), flags=re.I) or "Напоминание"
            return Route("tool", "reminder_create", {"text": what, "minutes": round(minutes, 2)},
                         why="Локальный планировщик")
    m = re.match(r"\s*напомни(?:\s+мне)?\s+через\s+(час|полчаса|минуту)\s*(.*)$", raw, re.I | re.S)
    if m:
        minutes = {"час": 60, "полчаса": 30, "минуту": 1}[m.group(1).casefold()]
        what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(2).strip(), flags=re.I) or "Напоминание"
        return Route("tool", "reminder_create", {"text": what, "minutes": minutes}, why="Локальный планировщик")
    m = re.match(r"\s*напомни(?:\s+мне)?\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)$", raw, re.I | re.S)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        import datetime as _dt
        now_ = _dt.datetime.now()
        due = now_.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if due <= now_:
            due += _dt.timedelta(days=1)
        what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(3).strip(), flags=re.I) or "Напоминание"
        return Route("tool", "reminder_create", {"text": what, "minutes": round((due - now_).total_seconds() / 60, 2)},
                     why="Локальный планировщик")
    m = re.match(r"\s*remind me in (\d+) (minute|minutes|hour|hours) (?:to )?(.*)$", raw, re.I)
    if m:
        n = int(m.group(1)) * (60 if m.group(2).startswith("hour") else 1)
        return Route("tool", "reminder_create", {"text": m.group(3) or "Reminder", "minutes": n},
                     why="Local scheduler")

    # --- file search ----------------------------------------------------------------
    m = re.fullmatch(r"(?:найди|поищи|ищи|где (?:лежит|находится)|find|search(?: for)?)\s+(?:мне\s+)?"
                     r"(?:файл\w*|документ\w*|папк\w*|file|files|folder)\s+(.+)", t)
    if m:
        return Route("tool", "search_files", {"query": _clean_query(m.group(1))}, why="Поиск по разрешённым папкам")
    m = re.fullmatch(r"(?:где (?:лежит|лежат|находится)|where is)\s+(.+)", t)
    if m:
        return Route("tool", "search_files", {"query": _clean_query(m.group(1))}, why="Поиск по разрешённым папкам")
    m = re.fullmatch(r"(?:найди|поищи)\s+(.+?)\s+в\s+(документах|загрузках|проектах|изображениях)", t)
    if m:
        folder = {"документах": "~/Documents", "загрузках": "~/Downloads", "проектах": "~/Projects",
                  "изображениях": "~/Pictures"}[m.group(2)]
        return Route("tool", "search_files", {"query": _clean_query(m.group(1)),
                                              "folder": os.path.expanduser(folder)}, why="Поиск в папке")

    # --- Flora workflow --------------------------------------------------------
    if re.search(r"\bфлор[аеуыой]?\b|\bflora\b", t):
        return Route("flora", args={"text": raw}, why="Проект «Флора» (демо-данные)", level=1)

    # --- open -------------------------------------------------------------------------
    m = re.fullmatch(r"(?:открой|запусти|включи|покажи|open|launch|start|run)\s+(.+)", t)
    if m:
        target = re.sub(r"^(?:приложение|программу|app)\s+", "", m.group(1)).strip()
        target = re.sub(r"\s+(?:пожалуйста|please)$", "", target)
        # ordinal reference to the last file list: «открой второй»
        om = re.fullmatch(r"(?:(?:мне )?(?:файл |результат |номер ))?(\S+)(?: (?:файл|результат|документ))?", target)
        last = ctx.get("last_files") or []
        if om and last:
            word = om.group(1)
            idx = len(last) if word in ("последний", "last") else _ordinal(word)
            if idx and 1 <= idx <= len(last):
                return Route("tool", "open_path", {"path": last[idx - 1]}, why="Ссылка на прошлый результат")
        if target in APP_ALIASES:
            return Route("tool", "app_open", {"app": APP_ALIASES[target]}, why="Список разрешённых приложений")
        folder_key = re.sub(r"^(?:папку|папка|folder)\s+", "", target)
        if folder_key in FOLDERS:
            p = Path(os.path.expanduser(FOLDERS[folder_key]))
            return Route("tool", "open_path", {"path": str(p)}, why="Известная папка")
        fm = re.fullmatch(r"(?:файл|документ|file)\s+(.+)", target)
        if fm:
            return Route("search_open", "search_files", {"query": _clean_query(fm.group(1))},
                         why="Поиск файла, затем открытие")
    return None


def _clean_query(q: str) -> str:
    q = re.sub(r"\s+(?:пожалуйста|please)$", "", q.strip())
    q = re.sub(r"^(?:с названием|под названием|named|called)\s+", "", q)
    return q.strip()
