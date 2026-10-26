"""Natural language → architecture commands, without AI (Russian; the way people say it and the way the local
recogniser writes it: «сорок сантиметров», «3,5 м», «полтора метра», «кухней», «в гостиную»).

    text → intent → object resolution (rooms by name in any case, «эта стена» = the selection in the window, ids,
    «дверь между кухней и гостиной») → operations → (the service) validation → execution → verification.

Several matching objects → never a guess: a question with numbered options («Какую дверь: 1) … 2) …?»), answered
by «первую», «вторую», an id, or the rooms. Not understood → None: the request goes to JARVIS's conversation (Claude),
which has architect_inspect / architect_edit with the same operations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import geometry as g
from .catalog import FURNITURE, MATERIALS, ROOM_KINDS, furniture_kind, material_of, nominative, norm, room_kind
from .model import ArchModel, pt


@dataclass
class Parsed:
    kind: str = "edit"            # edit | build | view | save | export | render | undo | info | question
    ops: list[dict] = field(default_factory=list)
    say: str = ""                 # what JARVIS understood, in words
    question: dict | None = None  # {"text", "options": [{"label", "ops"}]}
    view: dict | None = None      # {"mode": top|persp|walk|level|focus, "level"?, "target"?}
    args: dict = field(default_factory=dict)


# ---------------------------------------------------------------- numbers and lengths

UNITS = {"мм": 1.0, "миллиметр": 1.0, "mm": 1.0, "см": 10.0, "сантиметр": 10.0, "cm": 10.0, "м": 1000.0,
         "метр": 1000.0, "m": 1000.0, "дм": 100.0, "дециметр": 100.0}
ONES = {"ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3,
        "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8,
        "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10, "одиннадцать": 11, "двенадцать": 12,
        "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
        "восемнадцать": 18, "девятнадцать": 19}
TENS = {"двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30, "сорок": 40, "сорока": 40, "пятьдесят": 50,
        "пятидесяти": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90}
HUNDREDS = {"сто": 100, "ста": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
            "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
_NUMW = set(ONES) | set(TENS) | set(HUNDREDS) | {"тысяча", "тысячи", "тысяч", "полтора", "полторы", "половиной"}
_UNIT_RE = r"(?:мм|миллиметр\w*|см|сантиметр\w*|дм|дециметр\w*|метр\w*|м|mm|cm|m)(?![а-яa-z])"
_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + _UNIT_RE + r")?")


def _unit(u: str | None) -> float | None:
    if not u:
        return None
    u = u.casefold()
    for k in sorted(UNITS, key=len, reverse=True):
        if u.startswith(k):
            return UNITS[k]
    return None


def _words(tokens: list[str]) -> float | None:
    total, cur, seen = 0.0, 0.0, False
    for t in tokens:
        if t in ("полтора", "полторы"):
            cur += 1.5
        elif t == "половиной":
            cur += 0.5
        elif t in HUNDREDS:
            cur += HUNDREDS[t]
        elif t in TENS:
            cur += TENS[t]
        elif t in ONES:
            cur += ONES[t]
        elif t.startswith("тысяч"):
            total += (cur or 1) * 1000
            cur = 0
        else:
            continue
        seen = True
    return total + cur if seen else None


def lengths(text: str) -> list[float]:
    """Every length said in the text, in mm, in order («на 40 см», «3,5 метра», «сорок сантиметров», «полметра»)."""
    t = norm(text)
    out: list[tuple[int, float]] = []
    for m in _NUM_RE.finditer(t):
        v = float(m.group(1).replace(",", "."))
        k = _unit(m.group(2))
        if k is None:
            if re.match(r"\s*(?:градус|°|%|процент|шт|раз|этаж)", t[m.end():]):
                continue
            k = 1000.0 if v <= 10 else 10.0 if v <= 600 else 1.0       # «на 40» = cm, «3» = m, «2700» = mm
        out.append((m.start(), v * k))
    for m in re.finditer(r"\bпол(?:-)?(метра|сантиметра)\b", t):
        out.append((m.start(), 500.0 if m.group(1) == "метра" else 5.0))
    toks = list(re.finditer(r"[а-яa-z]+", t))
    i = 0
    while i < len(toks):
        if toks[i].group(0) in _NUMW:
            j = i
            seq = []
            while j < len(toks) and (toks[j].group(0) in _NUMW or toks[j].group(0) in ("с", "и")):
                seq.append(toks[j].group(0))
                j += 1
            v = _words(seq)
            unit = toks[j].group(0) if j < len(toks) else ""
            k = _unit(unit) if re.fullmatch(_UNIT_RE, unit) else None
            if v is not None and k is not None:
                out.append((toks[i].start(), v * k))
            i = max(j, i + 1)
            continue
        if toks[i].group(0) in ("метр", "сантиметр", "миллиметр") and (i == 0 or toks[i - 1].group(0) in ("на", "в", "до")):
            out.append((toks[i].start(), {"метр": 1000.0, "сантиметр": 10.0, "миллиметр": 1.0}[toks[i].group(0)]))
        i += 1
    return [v for _, v in sorted(out)]


def degrees(text: str) -> float | None:
    m = re.search(r"(\d+)\s*(?:градус|°)", norm(text))
    if m:
        return float(m.group(1))
    if re.search(r"\b(?:девяносто) градус", norm(text)):
        return 90.0
    return None


# ---------------------------------------------------------------- object resolution

ORD = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}


def ordinal(text: str) -> int | None:
    t = norm(text)
    m = re.search(r"(?:^|\s)(?:номер\s+)?(\d)(?:\s|$|-?(?:й|я|ю|ое|ая|ую))", t)
    if m:
        return int(m.group(1))
    for k, v in ORD.items():
        if not k.isdigit() and re.search(r"(?:^|\s)" + k + r"\w*", t):
            return v
    return None


def _stem_hit(text: str, stems) -> int:
    best = -1
    for s in stems:
        m = re.search(r"(?<![а-яa-z])" + re.escape(s[:5] if len(s) > 5 else s), text)
        if m and (best < 0 or m.start() < best):
            best = m.start()
    return best


def rooms_in(text: str, m: ArchModel) -> list[tuple[int, list[dict]]]:
    """Rooms named in the text, in order of mention: [(position, [matching rooms])] — a kind word («спальне») may
    match several rooms (Спальня, Спальня 2)."""
    t = norm(text)
    out = []
    taken: list[tuple[int, int]] = []
    rooms = m.data["rooms"]
    # exact names first («Кухня-гостиная», «Детская 2»)
    for r in sorted(rooms, key=lambda r: -len(r["name"])):
        n = norm(r["name"])
        stem = n[:-2] if len(n) > 5 else n
        i = t.find(stem)
        if i >= 0 and not any(a <= i < b for a, b in taken) and len(stem) >= 3:
            # «спальня 2» vs «спальня»: a number after the name must match
            num = re.match(r"\w*\s*(\d)", t[i + len(stem):])
            rn = re.search(r"(\d+)$", r["name"])
            if num and (not rn or rn.group(1) != num.group(1)):
                continue
            out.append((i, [r]))
            taken.append((i, i + len(stem) + 3))
    for kind, (_, stems) in ROOM_KINDS.items():
        i = _stem_hit(t, stems)
        if i < 0 or any(a <= i < b for a, b in taken):
            continue
        hits = [r for r in rooms if r.get("kind") == kind or room_kind(r["name"]) == kind]
        if kind in ("kitchen", "living"):
            hits += [r for r in rooms if r.get("kind") == "kitchen_living" and r not in hits]
        if hits:
            out.append((i, hits))
            taken.append((i, i + 6))
    m2 = re.search(r"room_(\d{3,})|комнат\w* (?:номер )?(\d+)", t)
    if m2:
        rid = f"ROOM_{int(m2.group(1) or m2.group(2)):03d}"
        r = m.get(rid)
        if r:
            out.append((m2.start(), [r]))
    out.sort(key=lambda x: x[0])
    return out


def ids_in(text: str, m: ArchModel) -> list[dict]:
    out = []
    for mm in re.finditer(r"\b(WALL|ROOM|DOOR|WINDOW|STAIR|COLUMN|SLAB|ROOF|FURN|LEVEL)[ _-]?(\d{1,4})\b", text, re.I):
        e = m.get(f"{mm.group(1).upper()}_{int(mm.group(2)):03d}")
        if e:
            out.append(e)
    t = norm(text)
    for word, prefix in (("стен", "WALL"), ("двер", "DOOR"), ("окн", "WINDOW"), ("окон", "WINDOW")):
        for mm in re.finditer(word + r"\w*\s+(?:номер\s+)?(\d{1,3})\b(?!\s*(?:см|мм|м\b|метр|санти|милли))", t):
            e = m.get(f"{prefix}_{int(mm.group(1)):03d}")
            if e and e not in out:
                out.append(e)
    return out


def describe(m: ArchModel, e: dict) -> str:
    names = {r["id"]: r["name"] for r in m.data["rooms"]}
    if e["type"] in ("door", "window"):
        sides = [names.get(x, "улица" if x == "outside" else x) for x in e.get("rooms", [])]
        return f"{e['id']} ({' — '.join(sides) or 'без помещений'}, {e['width'] / 10:g} см)"
    if e["type"] == "wall":
        sides = [names.get(x, x) for x in e.get("rooms", [])] or ["снаружи"]
        return f"{e['id']} ({' / '.join(sides)}, {m.wall_len(e) / 1000:.1f} м)"
    if e["type"] == "furniture":
        return f"{e['name']} {e['id']} ({names.get(e.get('room'), 'вне помещений')})"
    if e["type"] == "room":
        return f"{e['name']} ({e['area_m2']} м²)"
    return e["id"]


def choose(m: ArchModel, cands: list[dict], what: str, make_ops) -> Parsed:
    opts = [{"label": describe(m, e), "id": e["id"], "ops": make_ops(e)} for e in cands[:6]]
    text = f"{what}: " + "; ".join(f"{i}) {o['label']}" for i, o in enumerate(opts, 1)) + "?"
    return Parsed(kind="question", question={"kind": "choose", "text": text, "options": opts}, say=text)


def ask(text: str) -> Parsed:
    return Parsed(kind="question", question={"kind": "need", "text": text, "options": []}, say=text)


# ---------------------------------------------------------------- the commands

VERB_RM = r"(?:убери|удали|снеси|сломай|демонтируй|разбери|убрать|удалить|снести|заложи|уберите|удалите)"
VERB_MOVE = r"(?:перемести|сдвинь|подвинь|передвинь|перенеси|переставь|сместь|сместите|сдвиньте|подвиньте|переместите)"
VERB_ADD = r"(?:добавь|поставь|размести|положи|установи|добавьте|поставьте|добавить|поставить|вставь|прорежь|сделай)"
DIRS = {"вправо": (1, 0), "направо": (1, 0), "влево": (-1, 0), "налево": (-1, 0), "вверх": (0, 1), "наверх": (0, 1),
        "выше": (0, 1), "вниз": (0, -1), "ниже": (0, -1), "вперед": (0, 1), "назад": (0, -1)}

BUILD_RE = re.compile(r"(?:сделай|построй|создай|преврати|переведи|сгенерируй|собери|сделать|построить|сделайте|"
                      r"постройте)\b.*\b(?:3\s?d|3\s?д|три\s?д[еэ]?|трехмерн\w*|трёхмерн\w*|объемн\w*|модел\w*)|"
                      r"(?:3\s?d|3\s?д|трехмерн\w*)\s+(?:модел\w*\s+)?(?:по|из)\s+(?:этого|этому|плана|чертеж\w*|рисунк\w*|эскиз\w*)|"
                      r"(?:распознай|разбери|проанализируй)\s+(?:этот\s+)?(?:план|чертеж\w*|эскиз\w*|рисунок)")


def is_build(text: str) -> bool:
    return bool(BUILD_RE.search(norm(text)))


def parse(text: str, m: ArchModel | None, selected: str | None = None) -> Parsed | None:
    """The command in text, or None when it is not about the building (or not understood)."""
    t = norm(text)
    t = re.sub(r"^(?:джарвис|jarvis)[\s,.!:—-]*", "", t)
    if is_build(t):
        return Parsed(kind="build", say="Строю 3D-модель по плану")
    if m is None:
        return None
    sel = m.get(selected) if selected else None

    def this(kind: tuple) -> dict | None:
        if sel and sel["type"] in kind and re.search(r"\b(?:эт[уоиа]|этот|выбранн\w*|выделенн\w*)\b", t):
            return sel
        ids = [e for e in ids_in(text, m) if e["type"] in kind]
        return ids[0] if len(ids) == 1 else None

    # --- view / navigation -------------------------------------------------------
    if re.match(r"(?:покажи|открой|переключи(?:сь)? на|перейди на)\s", t):
        lv = re.search(r"(перв|втор|трет|1|2|3)\w*\s*(?:-?й\s*)?этаж", t)
        if lv:
            n = ORD.get(lv.group(1)[:5].rstrip("ыоуеая"), None) or ORD.get(lv.group(1), 1)
            n = {"перв": 1, "втор": 2, "трет": 3}.get(lv.group(1)[:4], n)
            levels = sorted(m.data["levels"], key=lambda x: x["elevation"])
            if n > len(levels):
                return ask(f"В модели {len(levels)} этаж(а) — {n}-го нет.")
            L = levels[n - 1]
            return Parsed(kind="view", view={"mode": "level", "level": L["id"]}, say=f"Показываю: {L['name']}")
        if re.search(r"сверху|план\w*$|вид сверху|с высоты", t):
            return Parsed(kind="view", view={"mode": "top"}, say="Вид сверху")
        if re.search(r"изнутри|от первого лица|внутри|интерьер|прогул|walk", t):
            rs = rooms_in(t, m)
            target = rs[0][1][0]["id"] if rs and len(rs[0][1]) == 1 else None
            return Parsed(kind="view", view={"mode": "walk", "target": target},
                          say="Вид изнутри" + (f": {m.get(target)['name']}" if target else ""))
        if re.search(r"3\s?d|3\s?д|модел|дом|здани|снаружи|целиком", t):
            return Parsed(kind="view", view={"mode": "persp"}, say="Модель целиком")
        rs = rooms_in(t, m)
        if rs and len(rs[0][1]) == 1:
            r = rs[0][1][0]
            return Parsed(kind="view", view={"mode": "focus", "target": r["id"]}, say=f"Показываю: {r['name']}")
    # --- save / export / render / undo -----------------------------------------------
    fm = re.search(r"\b(glb|gltf|obj|step|stp|stl|dxf|blend|blender|freecad|fcstd|fbx|brep)\b", t)
    if fm and re.search(r"экспорт|выгрузи|сохрани|экспортируй|выведи|запиши|сделай файл|в формат", t):
        f = {"stp": "step", "blender": "blend", "freecad": "fcstd"}.get(fm.group(1), fm.group(1))
        return Parsed(kind="export", args={"formats": [f]}, say=f"Экспорт в {f.upper()}")
    if re.fullmatch(r"(?:сохрани|сохранить|сохраните)(?: (?:этот |мой )?(?:проект|модель|дом|план|всё|все))?", t):
        return Parsed(kind="save", say="Сохраняю проект")
    if re.search(r"\b(?:рендер\w*|визуализаци\w*|красив\w* (?:картинк|изображени)\w*|фотореалист\w*)", t):
        rs = rooms_in(t, m)
        target = rs[0][1][0]["id"] if rs and len(rs[0][1]) == 1 else None
        view = "top" if "сверху" in t else "room" if target else "persp"
        return Parsed(kind="render", args={"view": view, "room": target}, say="Делаю визуализацию")
    if re.fullmatch(r"(?:отмени|верни|откати)(?: последн\w*)? (?:изменени\w*|правк\w*|действи\w*)|верни (?:как было|обратно)|"
                    r"отмени (?:это|последнее)", t):
        return Parsed(kind="undo", say="Отменяю последнее изменение")
    # --- information ----------------------------------------------------------------
    if re.search(r"\b(?:какая|какой|сколько)\b.*\b(?:площад\w*|метров|размер\w*)", t):
        rs = rooms_in(t, m)
        if rs and len(rs[0][1]) == 1:
            r = rs[0][1][0]
            net = f", чистый пол {r['net_area_m2']} м²" if r.get("net_area_m2") else ""
            return Parsed(kind="info", say=f"{r['name']}: {r['area_m2']} м² по осям стен{net}")
        total = sum(r["area_m2"] for r in m.data["rooms"])
        net = sum(r.get("net_area_m2") or 0 for r in m.data["rooms"])
        return Parsed(kind="info", say=f"Помещений: {len(m.data['rooms'])}, площадь по осям {total:.1f} м²"
                      + (f", чистая {net:.1f} м²" if net else ""))
    # --- ceiling / wall height -----------------------------------------------------------
    if re.search(r"высот\w* (?:потолк\w*|стен\w*|этаж\w*|помещени\w*|комнат\w*)|потол\w* (?:на |в |до )?\d|"
                 r"(?:сделай|поставь) потолк\w*", t):
        L = lengths(t)
        if not L:
            return ask("Какую высоту поставить? Например, «высота потолка 3 метра».")
        h = L[-1]
        w = this(("wall",))
        if w and re.search(r"\bстен", t):
            return Parsed(ops=[{"op": "set_wall", "id": w["id"], "height": h}], say=f"Высота {w['id']}: {h / 1000:g} м")
        lv = m.data["levels"][0]["id"] if len(m.data["levels"]) == 1 else None
        if lv is None:
            n = ordinal(t)
            levels = sorted(m.data["levels"], key=lambda x: x["elevation"])
            lv = levels[n - 1]["id"] if n and n <= len(levels) else None
            if lv is None:
                return choose(m, levels, "Какой этаж", lambda e: [{"op": "set_level", "id": e["id"], "height": h}])
        return Parsed(ops=[{"op": "set_level", "id": lv, "height": h}], say=f"Высота потолка {h / 1000:g} м")
    # --- walls ----------------------------------------------------------------------------
    rm_between = re.search(VERB_RM + r"\b.*\bстен\w*\s+между\s+(.+?)\s+и\s+(.+)$", t) or \
        re.search(r"(?:объедини|соедини|совмести|объединить)\s+(.+?)\s+(?:и|с|со)\s+(.+)$", t)
    if rm_between:
        rs = rooms_in(rm_between.group(1), m), rooms_in(rm_between.group(2), m)
        if not rs[0] or not rs[1]:
            missing = rm_between.group(1) if not rs[0] else rm_between.group(2)
            return ask(f"Не нашёл в модели помещение «{missing.strip()}». Помещения: "
                       + ", ".join(r["name"] for r in m.data["rooms"]) + ".")
        A, B = rs[0][0][1], rs[1][0][1]
        if len(A) == 1 and len(B) == 1 and A[0]["id"] == B[0]["id"]:
            return Parsed(kind="info", say=f"Это уже одно помещение — «{A[0]['name']}», стены между ними нет")
        pairs = [(a, b) for a in A for b in B if a["id"] != b["id"] and m.shared(a, b)]
        if not pairs:
            return ask(f"У «{A[0]['name']}» и «{B[0]['name']}» нет общей стены.")
        if len(pairs) > 1:
            opts = [{"label": f"{a['name']} и {b['name']}", "id": a["id"] + "|" + b["id"],
                     "ops": [{"op": "remove_wall_between", "a": a["id"], "b": b["id"]}]} for a, b in pairs[:6]]
            text = "Между какими помещениями: " + "; ".join(f"{i}) {o['label']}" for i, o in enumerate(opts, 1)) + "?"
            return Parsed(kind="question", question={"kind": "choose", "text": text, "options": opts}, say=text)
        a, b = pairs[0]
        walls = sorted({w["id"] for w, s, e in m.shared(a, b)})
        return Parsed(ops=[{"op": "remove_wall_between", "a": a["id"], "b": b["id"]}],
                      say=f"Убираю стену между «{a['name']}» и «{b['name']}» ({', '.join(walls)})")
    if re.search(VERB_RM + r"\b.*\b(?:стен\w*|перегородк\w*)\b", t):
        w = this(("wall",))
        if w:
            return Parsed(ops=[{"op": "delete_wall", "id": w["id"]}], say=f"Убираю стену {w['id']}")
        rs = rooms_in(t, m)
        if len(rs) == 1 and len(rs[0][1]) == 1:
            r = rs[0][1][0]
            inner = [m.get(x) for x in r.get("walls", []) if m.get(x) and m.get(x).get("kind") != "exterior"]
            if len(inner) == 1:
                return Parsed(ops=[{"op": "delete_wall", "id": inner[0]["id"]}], say=f"Убираю стену {inner[0]['id']}")
            if inner:
                return choose(m, inner, "Какую стену убрать", lambda e: [{"op": "delete_wall", "id": e["id"]}])
        return ask("Какую стену убрать? Назовите помещения, между которыми она стоит («между кухней и гостиной»), "
                   "или выберите стену в разделе «Архитектура».")
    split = re.search(r"(?:раздели|разделить|поделить|подели)\s+(.+?)\s+(?:пополам|на две|на 2|стеной|перегородкой)", t) or \
        re.search(r"(?:добавь|поставь|построй)\s+(?:стену|перегородку)\s+(?:в|посередине|посреди)\s+(.+)$", t)
    if split:
        rs = rooms_in(split.group(1), m)
        if not rs:
            return ask("Какое помещение разделить?")
        if len(rs[0][1]) > 1:
            return choose(m, rs[0][1], "Какое помещение разделить", lambda e: [{"op": "split_room", "id": e["id"]}])
        r = rs[0][1][0]
        return Parsed(ops=[{"op": "split_room", "id": r["id"]}], say=f"Делю «{r['name']}» перегородкой")
    if re.search(r"толщин\w*", t) and re.search(r"стен", t):
        L = lengths(t)
        if not L:
            return ask("Какую толщину стен поставить? Например, «толщина наружных стен 40 сантиметров».")
        th = L[-1]
        w = this(("wall",))
        if w:
            return Parsed(ops=[{"op": "set_wall", "id": w["id"], "thickness": th}], say=f"Толщина {w['id']}: {th / 10:g} см")
        kind = "exterior" if re.search(r"наружн|внешн|фасад", t) else "interior" if re.search(r"внутрен|межкомн|перегород", t) else None
        return Parsed(ops=[{"op": "set_walls", "filter": {"kind": kind} if kind else {}, "thickness": th}],
                      say=f"Толщина {'наружных' if kind == 'exterior' else 'внутренних' if kind else 'всех'} стен: {th / 10:g} см")
    if re.search(VERB_MOVE + r"\s+(?:эту\s+|выбранную\s+)?стен\w*", t):
        w = this(("wall",))
        if not w:
            return ask("Какую стену сдвинуть? Выберите её в разделе «Архитектура» или назовите номер, например «стену 3».")
        L = lengths(t)
        d = next((v for k, v in DIRS.items() if k in t), None)
        if not L or not d:
            return ask("На сколько и куда сдвинуть стену? Например, «на 50 сантиметров вправо».")
        return Parsed(ops=[{"op": "move_wall", "id": w["id"], "dx": d[0] * L[0], "dy": d[1] * L[0]}],
                      say=f"Сдвигаю {w['id']} на {L[0] / 10:g} см")
    # --- doors and windows ------------------------------------------------------------
    om = re.search(r"\b(двер\w*|окн\w*|окон\w*|проем\w*)", t)
    if om and not furniture_kind(t):
        typ = "door" if om.group(1).startswith(("двер", "проем")) else "window"
        word = "дверь" if typ == "door" else "окно"
        pool = m.data["doors"] if typ == "door" else m.data["windows"]

        def resolve() -> tuple[list[dict], str]:
            e = this((typ,))
            if e:
                return [e], ""
            rs = rooms_in(t, m)
            c = list(pool)
            where = ""
            if len(rs) >= 2:
                A, B = {r["id"] for r in rs[0][1]}, {r["id"] for r in rs[1][1]}
                c = [o for o in c if A & set(o.get("rooms", [])) and B & set(o.get("rooms", []))]
                where = f" между «{rs[0][1][0]['name']}» и «{rs[1][1][0]['name']}»"
            elif len(rs) == 1:
                A = {r["id"] for r in rs[0][1]}
                c = [o for o in c if A & set(o.get("rooms", []))]
                where = f" в «{rs[0][1][0]['name']}»"
                if typ == "door" and re.search(r"входн|наружн|с улиц", t):
                    c = [o for o in c if "outside" in o.get("rooms", [])]
            elif typ == "door" and re.search(r"входн|наружн|с улиц", t):
                c = [o for o in c if "outside" in o.get("rooms", [])]
            return c, where

        if re.match(VERB_MOVE, t):
            c, where = resolve()
            L = lengths(t)
            d = next((v for k, v in DIRS.items() if re.search(r"\b" + k + r"\b", t)), None)
            if not c:
                return ask(f"Не нашёл {word}{where}.")
            if not L:
                return ask(f"На сколько сдвинуть {word}? Например, «на 40 сантиметров вправо».")

            def move_ops(o, L=L, d=d):
                w = m.get(o["host"])
                u = g.unit(g.sub(pt(w["end"]), pt(w["start"])))
                if d is None:
                    return [{"op": "move_opening", "id": o["id"], "by": L[0]}]
                along = g.dot(u, d)
                if abs(along) < 0.3:
                    return []
                return [{"op": "move_opening", "id": o["id"], "by": L[0] * (1 if along > 0 else -1)}]
            movable = [o for o in c if move_ops(o)]
            if len(movable) > 1:
                return choose(m, movable, f"Какую {word}" if typ == "door" else "Какое окно", move_ops)
            o = movable[0] if movable else c[0]
            ops = move_ops(o)
            if not ops and len(c) > 1:
                return ask(f"Ни одна подходящая {word if typ == 'door' else 'окно'} не двигается в эту сторону: "
                           "двери и окна сдвигаются только вдоль своей стены. Скажите «вверх» или «вниз».")
            if not ops:
                return ask(f"{o['id']} стоит в стене, которая идёт {'вертикально' if d[0] else 'горизонтально'} на плане: "
                           "сдвинуть можно только вдоль неё — " + ("вверх или вниз?" if d[0] else "влево или вправо?"))
            return Parsed(ops=ops, say=f"Сдвигаю {o['id']} на {L[0] / 10:g} см")
        sizing = re.search(r"шир|выс|\bуже\b|ниже|выше|больше|меньше|размер|увелич|уменьш|расшир|сузь", t)
        if re.match(r"(?:сделай|измени|увеличь|уменьши|расширь|сузь|поменяй|поставь)\b", t) and sizing:
            c, where = resolve()
            L = lengths(t)
            if not c:
                return ask(f"Не нашёл {word}{where}.")
            if not L:
                return ask(f"Какой размер? Например, «ширина {('двери 90' if typ == 'door' else 'окна 150')} сантиметров».")
            key = "height" if re.search(r"выс|выше|ниже", t) else "width"
            rel = re.search(r"\bна\s", t) and re.search(r"шире|уже|выше|ниже|увелич|уменьш|больше|меньше", t)
            sign = -1 if re.search(r"уже|ниже|уменьш|меньше|сузь", t) else 1

            def size_ops(o, L=L, key=key, rel=rel, sign=sign):
                v = float(o[key]) + sign * L[0] if rel else L[0]
                return [{"op": "set_opening", "id": o["id"], key: v}]
            if len(c) > 1:
                return choose(m, c, f"Какую {word}" if typ == "door" else "Какое окно", size_ops)
            o = c[0]
            return Parsed(ops=size_ops(o), say=f"{o['id']}: {'высота' if key == 'height' else 'ширина'} "
                                              f"{size_ops(o)[0][key] / 10:g} см")
        if re.match(VERB_RM, t):
            c, where = resolve()
            if not c:
                return ask(f"Не нашёл {word}{where}.")
            if len(c) > 1:
                return choose(m, c, f"Какую {word} убрать" if typ == "door" else "Какое окно убрать",
                              lambda o: [{"op": "delete_opening", "id": o["id"]}])
            return Parsed(ops=[{"op": "delete_opening", "id": c[0]["id"]}], say=f"Убираю {c[0]['id']}")
        if re.match(VERB_ADD, t):
            rs = rooms_in(t, m)
            L = lengths(t)
            width = L[0] if L else None
            kind = "opening" if om.group(1).startswith("проем") else typ
            if len(rs) >= 2:
                A, B = rs[0][1][0], rs[1][1][0]
                parts = m.shared(A, B)
                if not parts:
                    return ask(f"У «{A['name']}» и «{B['name']}» нет общей стены — дверь поставить некуда.")
                w, s, e = max(parts, key=lambda p: p[2] - p[1])
                return Parsed(ops=[{"op": "add_opening", "kind": kind, "host": w["id"], "offset": (s + e) / 2, "width": width}],
                              say=f"Добавляю {word} между «{A['name']}» и «{B['name']}»")
            if len(rs) == 1:
                r = rs[0][1][0]
                ext = [m.get(x) for x in r.get("walls", []) if m.get(x) and m.get(x).get("kind") == "exterior"]
                if typ == "window" or re.search(r"входн|на улиц|наружу", t):
                    if not ext:
                        return ask(f"У «{r['name']}» нет наружной стены — окно поставить некуда.")
                    w = max(ext, key=lambda w: m.wall_len(w) - sum(o["width"] for o in m.hosted(w["id"])))
                    return Parsed(ops=[{"op": "add_opening", "kind": kind, "host": w["id"], "offset": _free_spot(m, w, width or 1200),
                                        "width": width}], say=f"Добавляю {word} в «{r['name']}» (стена {w['id']})")
                return ask(f"Куда ведёт новая дверь из «{r['name']}»? Скажите «между {r['name'].lower()} и …».")
            w = this(("wall",))
            if w:
                return Parsed(ops=[{"op": "add_opening", "kind": kind, "host": w["id"], "offset": _free_spot(m, w, width or 900),
                                    "width": width}], say=f"Добавляю {word} в {w['id']}")
            return ask(f"Куда поставить {word}? Назовите помещение или помещения по обе стороны.")
    # --- furniture --------------------------------------------------------------------
    fk = furniture_kind(t)
    if fk and material_of(t) and re.search(r"\b(?:пол|полы|стен\w*|потол\w*)\b", t) and \
            not re.match(VERB_ADD + r"\s+(?!пол)", t.replace("сделай ", "")):
        fk = None                       # «сделай пол в кухне плиткой»: a finish, not a stove
    if fk:
        name = FURNITURE[fk]["name"].lower()
        items = [f for f in m.data["furniture"] if f["kind"] == fk]
        rs = rooms_in(t, m)
        opp = re.search(r"напротив\s+(.+)$", t)
        if re.match(VERB_RM, t) or re.match(r"(?:выкинь|выброси|убрать)", t):
            if rs:
                ids = {r["id"] for r in rs[0][1]}
                items = [f for f in items if f.get("room") in ids]
            sel_f = this(("furniture",))
            if sel_f:
                items = [sel_f]
            if not items:
                return ask(f"В модели нет: {name}.")
            if len(items) > 1:
                return choose(m, items, f"Что убрать ({name})", lambda f: [{"op": "delete_furniture", "id": f["id"]}])
            return Parsed(ops=[{"op": "delete_furniture", "id": items[0]["id"]}], say=f"Убираю: {name} {items[0]['id']}")
        if re.match(r"(?:поверни|разверни|повернуть|разверните)", t):
            sel_f = this(("furniture",))
            items = [sel_f] if sel_f else ([f for f in items if not rs or f.get("room") in {r["id"] for r in rs[0][1]}])
            if not items:
                return ask(f"В модели нет: {name}.")
            ang = degrees(t) or (180.0 if re.search(r"другую сторону|обратно|задом", t) else 90.0)
            if len(items) > 1:
                return choose(m, items, f"Что повернуть ({name})", lambda f: [{"op": "move_furniture", "id": f["id"], "rotate_by": ang}])
            return Parsed(ops=[{"op": "move_furniture", "id": items[0]["id"], "rotate_by": ang}], say=f"Поворачиваю: {name}")
        if re.match(VERB_MOVE, t):
            sel_f = this(("furniture",))
            if sel_f:
                items = [sel_f]
            elif rs and len(rs) >= 2:
                items = [f for f in items if f.get("room") in {r["id"] for r in rs[0][1]}] or items
            if not items:
                return ask(f"В модели нет: {name}. Добавить?")
            L = lengths(t)
            d = next((v for k, v in DIRS.items() if re.search(r"\b" + k + r"\b", t)), None)
            to_room = None
            mm = re.search(r"\b(?:в|во|на)\s+(\w+)\s*$", t)
            if mm and not L:
                rr = rooms_in(mm.group(1), m)
                to_room = rr[0][1][0] if rr and len(rr[0][1]) == 1 else None

            def mv(f, L=L, d=d, to_room=to_room):
                if to_room is not None:
                    return [{"op": "move_furniture", "id": f["id"], "room": to_room["id"]}]
                return [{"op": "move_furniture", "id": f["id"], "dx": d[0] * L[0], "dy": d[1] * L[0]}]
            if to_room is None and (not L or not d):
                return ask(f"Куда переставить {name}? Например, «на 50 сантиметров влево» или «в спальню».")
            if len(items) > 1:
                return choose(m, items, f"Что переставить ({name})", mv)
            return Parsed(ops=mv(items[0]), say=f"Переставляю: {name}")
        if re.match(VERB_ADD, t) or opp:
            op = {"op": "add_furniture", "kind": fk}
            if opp:
                ok = furniture_kind(opp.group(1))
                others = [f for f in m.data["furniture"] if f["kind"] == ok] if ok else []
                if rs:
                    ids = {r["id"] for r in rs[0][1]}
                    others = [f for f in others if f.get("room") in ids] or others
                if not others:
                    return ask(f"Напротив чего поставить? В модели нет: {opp.group(1).strip()}.")
                if len(others) > 1:
                    return choose(m, others, f"Напротив какого предмета поставить {name}",
                                  lambda f: [{**op, "opposite": f["id"]}])
                op["opposite"] = others[0]["id"]
                return Parsed(ops=[op], say=f"Ставлю {name} напротив: {others[0]['name'].lower()}")
            if rs:
                if len(rs[0][1]) > 1:
                    return choose(m, rs[0][1], f"В какое помещение поставить {name}", lambda r: [{**op, "room": r["id"]}])
                op["room"] = rs[0][1][0]["id"]
            mat = material_of(t)
            if mat:
                op["material"] = mat
            return Parsed(ops=[op], say=f"Добавляю {name}" + (f" в «{rs[0][1][0]['name']}»" if rs else ""))
    # --- materials ----------------------------------------------------------------------
    mat = material_of(t)
    if mat and re.search(r"(?:сделай|покрась|покрой|поменяй|замени|отделай|постели|положи|выкрась|пусть|сделать)|"
                         r"\b(?:пол|стены|потолок)\b\s*(?:—|-|будет|будут)", t):
        target = "floor" if re.search(r"\bпол(?:ы|ов)?\b|\bпокрыти|ламинат|паркет", t) and not re.search(r"\bстен", t) else \
            "ceiling" if re.search(r"потол", t) else "walls" if re.search(r"стен", t) else None
        if target is None:
            sel_e = sel if sel and re.search(r"\b(?:эт[уоиа]|этот|выбранн\w*)\b", t) else None
            if sel_e:
                return Parsed(ops=[{"op": "set_material", "target": sel_e["id"], "material": mat}],
                              say=f"{sel_e['id']}: {MATERIALS[mat]['name'].lower()}")
            return ask("Что отделать этим материалом — стены, пол или потолок?")
        if target == "walls" and re.search(r"наружн|фасад|снаружи", t):
            target = "exterior_walls"
        elif target == "walls" and re.search(r"внутрен|изнутри", t):
            target = "interior_walls"
        rs = rooms_in(t, m)
        room = None
        if rs:
            if len(rs[0][1]) > 1:
                return choose(m, rs[0][1], "В каком помещении", lambda r: [{"op": "set_material", "target": target,
                                                                           "material": mat, "room": r["id"]}])
            room = rs[0][1][0]["id"]
        what = {"floor": "Пол", "ceiling": "Потолок", "walls": "Стены", "exterior_walls": "Наружные стены",
                "interior_walls": "Внутренние стены"}[target]
        return Parsed(ops=[{"op": "set_material", "target": target, "material": mat, **({"room": room} if room else {})}],
                      say=f"{what}{' в «' + m.get(room)['name'] + '»' if room else ''}: {MATERIALS[mat]['name'].lower()}")
    # --- rooms ----------------------------------------------------------------------------
    rn = re.search(r"(?:переименуй|назови)\s+(.+?)\s+(?:в|как)\s+(.+)$", t)
    if rn:
        rs = rooms_in(rn.group(1), m)
        if not rs:
            return ask(f"Не нашёл помещение «{rn.group(1)}».")
        if len(rs[0][1]) > 1:
            return choose(m, rs[0][1], "Какое помещение переименовать",
                          lambda r: [{"op": "rename_room", "id": r["id"], "name": nominative(rn.group(2))}])
        r = rs[0][1][0]
        return Parsed(ops=[{"op": "rename_room", "id": r["id"], "name": nominative(rn.group(2))}],
                      say=f"«{r['name']}» → «{nominative(rn.group(2))}»")
    if re.match(VERB_RM, t) and re.search(r"комнат|помещени|" + "|".join(s[0][:5] for s in ROOM_KINDS.values()), t):
        rs = rooms_in(t, m)
        if rs:
            if len(rs[0][1]) > 1:
                return choose(m, rs[0][1], "Какое помещение убрать", lambda r: [{"op": "delete_room", "id": r["id"]}])
            r = rs[0][1][0]
            return Parsed(ops=[{"op": "delete_room", "id": r["id"]}], say=f"Убираю «{r['name']}» (объединяю с соседним)")
    ar = re.search(r"(?:добавь|пристрой|построй|сделай)\s+(?:нов\w+\s+)?(комнат\w*|" +
                   "|".join(s[0][:5] + r"\w*" for s in ROOM_KINDS.values()) + r")\b(.*)$", t)
    if ar and lengths(t):
        L = lengths(t)
        if len(L) < 2:
            return ask("Какого размера помещение? Например, «добавь кабинет 3 на 4 метра справа от гостиной».")
        side = "left" if re.search(r"слева|левее", t) else "top" if re.search(r"сверху|выше|над", t) else \
            "bottom" if re.search(r"снизу|ниже|под", t) else "right"
        rs = rooms_in(ar.group(2), m)
        kind = room_kind(ar.group(1))
        name = ROOM_KINDS.get(kind, ("Комната",))[0] if kind not in ("other", "room") else "Комната"
        op = {"op": "add_room", "name": name, "width": L[0], "depth": L[1], "side": side}
        if rs and len(rs[0][1]) == 1:
            op["next_to"] = rs[0][1][0]["id"]
        return Parsed(ops=[op], say=f"Добавляю помещение «{name}» {L[0] / 1000:g}×{L[1] / 1000:g} м")
    # --- roof / levels ----------------------------------------------------------------------
    if re.search(r"крыш", t):
        if re.match(VERB_RM, t) or re.search(r"без крыш|сними крыш", t):
            return Parsed(ops=[{"op": "set_roof", "kind": "none"}], say="Убираю крышу")
        if re.match(r"(?:добавь|верни|сделай|поставь)", t):
            return Parsed(ops=[{"op": "set_roof", "kind": "flat"}], say="Плоская крыша")
    if re.match(r"(?:добавь|пристрой|построй|надстрой)\s+(?:втор\w+\s+|ещ[её]\s+(?:один\s+)?)?этаж", t):
        return Parsed(ops=[{"op": "add_level"}], say="Добавляю этаж")
    return None


def _free_spot(m: ArchModel, w: dict, width: float) -> float:
    """The middle of the longest stretch of a wall without openings (mm from its start)."""
    L = m.wall_len(w)
    taken = sorted((o["offset"] - o["width"] / 2 - 150, o["offset"] + o["width"] / 2 + 150) for o in m.hosted(w["id"]))
    spans, cur = [], 150.0
    for a, b in taken:
        if a > cur:
            spans.append((cur, a))
        cur = max(cur, b)
    if cur < L - 150:
        spans.append((cur, L - 150))
    best = max(spans, key=lambda s: s[1] - s[0], default=(0, L))
    return (best[0] + best[1]) / 2


def answer(text: str, q: dict, m: ArchModel) -> dict | None:
    """The person's answer to a question JARVIS asked: {"ops": [...]} for a chosen option, {"yes": bool},
    {"length": mm}, {"scale": mm per px}; None when the phrase is not an answer."""
    t = norm(text)
    t = re.sub(r"^(?:джарвис|jarvis)[\s,.!:—-]*", "", t)
    if q.get("kind") == "choose":
        opts = q.get("options") or []
        for i, o in enumerate(opts):
            if o.get("id") and o["id"].casefold() in t.replace(" ", "_"):
                return {"ops": o["ops"], "label": o["label"]}
        n = ordinal(t)
        if n and 1 <= n <= len(opts) and len(t.split()) <= 4:
            return {"ops": opts[n - 1]["ops"], "label": opts[n - 1]["label"]}
        rs = rooms_in(t, m)
        if rs:
            groups = [{r["name"].casefold() for r in lst} for _, lst in rs]
            hit = [o for o in opts if all(any(nm in o["label"].casefold() for nm in grp) for grp in groups)]
            if len(hit) == 1:
                return {"ops": hit[0]["ops"], "label": hit[0]["label"]}
        if re.fullmatch(r"(?:нет|не надо|отмена|отмени|никакую|никакой|ничего)", t):
            return {"ops": [], "cancel": True}
        return None
    if q.get("kind") in ("scale", "confirm", "need"):
        if re.fullmatch(r"(?:да|давай|ага|угу|используй|подтверждаю|верно|хорошо|ок|окей|yes|оставь|оставить|"
                        r"да,? используй(?: предполагаемый)?(?: масштаб)?|используй предполагаемый(?: масштаб)?)", t):
            return {"yes": True}
        if re.fullmatch(r"(?:нет|не надо|не используй|убери|удали|no)", t):
            return {"yes": False}
        if q.get("kind") == "scale":
            mm = re.search(r"1\s*:\s*(\d+)", t)
            if mm:
                return {"ratio": int(mm.group(1))}
            L = lengths(t)
            if L:
                wid = [e for e in ids_in(text, m) if e["type"] == "wall"]
                which = "width" if re.search(r"ширин|длин\w* дома|по горизонтал|в ширину", t) else \
                    "depth" if re.search(r"глубин|высот\w* плана|по вертикал|в глубину", t) else "width"
                return {"length": L[0], "wall": wid[0]["id"] if wid else None, "axis": which}
    return None
