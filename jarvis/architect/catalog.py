"""What JARVIS knows about rooms, furniture and materials: Russian names and spoken forms, default sizes, colours.
Furniture sizes are the usual catalogue sizes (mm); a furniture kind without a 3D model in the library is built
procedurally in Blender (blender_worker.FURNITURE)."""
from __future__ import annotations

import re

# ---------------------------------------------------------------- rooms
# kind: (name, stems that name it in speech or on a drawing)
ROOM_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "kitchen": ("Кухня", ("кухн", "kitchen", "кух.")),
    "living": ("Гостиная", ("гостин", "зал", "living", "общая комн", "салон", "lounge")),
    "kitchen_living": ("Кухня-гостиная", ("кухня-гостин", "кухня гостин", "студи")),
    "bedroom": ("Спальня", ("спальн", "bedroom", "спал")),
    "children": ("Детская", ("детск", "kids", "nursery")),
    "bathroom": ("Ванная", ("ванн", "санузел", "сан.узел", "с/у", "bath", "душев")),
    "toilet": ("Туалет", ("туалет", "уборн", "wc", "toilet")),
    "hall": ("Прихожая", ("прихож", "холл", "hall", "тамбур", "вестибюл", "entry")),
    "corridor": ("Коридор", ("коридор", "corridor")),
    "balcony": ("Балкон", ("балкон", "лоджи", "balcony")),
    "storage": ("Кладовая", ("кладов", "гардероб", "storage", "closet", "чулан")),
    "office": ("Кабинет", ("кабинет", "office", "study")),
    "dining": ("Столовая", ("столов", "dining")),
    "laundry": ("Постирочная", ("постироч", "прачечн", "laundry")),
    "technical": ("Котельная", ("котельн", "техпом", "тех. пом", "boiler")),
    "garage": ("Гараж", ("гараж", "garage")),
    "terrace": ("Терраса", ("террас", "веранд", "terrace", "porch")),
    "stairwell": ("Лестница", ("лестн", "stair")),
    "room": ("Комната", ("комнат", "room")),
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").casefold().replace("ё", "е")).strip()


def room_kind(name: str) -> str:
    """The kind a room name or a spoken word means («Кухня-гостиная» → kitchen_living, «гостиной» → living)."""
    n = norm(name)
    for kind in ("kitchen_living",) + tuple(k for k in ROOM_KINDS if k != "kitchen_living"):
        if any(s in n for s in ROOM_KINDS[kind][1]):
            return kind
    return "other"


def room_title(kind: str) -> str:
    return ROOM_KINDS.get(kind, ("Помещение", ()))[0]


# ---------------------------------------------------------------- furniture
# kind: name, spoken stems, default size (width along the wall, depth, height) mm, placement, material
FURNITURE: dict[str, dict] = {
    "sofa": {"name": "Диван", "say": ("диван", "sofa", "couch", "софа"), "size": (2100, 950, 850),
             "place": "wall", "material": "fabric_gray", "rooms": ("living", "kitchen_living")},
    "armchair": {"name": "Кресло", "say": ("кресл", "armchair"), "size": (850, 850, 850),
                 "place": "wall", "material": "fabric_gray", "rooms": ("living",)},
    "bed": {"name": "Кровать", "say": ("кроват", "bed", "постел"), "size": (1600, 2050, 900),
            "place": "wall_head", "material": "fabric_white", "rooms": ("bedroom", "children")},
    "chair": {"name": "Стул", "say": ("стул", "chair"), "size": (450, 500, 900),
              "place": "near", "material": "wood_oak", "rooms": ("kitchen", "dining")},
    "table": {"name": "Стол", "say": ("стол", "table"), "size": (1400, 800, 750),
              "place": "center", "material": "wood_oak", "rooms": ("kitchen", "dining", "living")},
    "desk": {"name": "Письменный стол", "say": ("письменн", "рабоч", "desk", "компьютерн"), "size": (1200, 650, 750),
             "place": "wall", "material": "wood_oak", "rooms": ("office", "bedroom", "children")},
    "wardrobe": {"name": "Шкаф", "say": ("шкаф", "гардероб", "wardrobe", "closet"), "size": (1600, 600, 2200),
                 "place": "wall", "material": "wood_white", "rooms": ("bedroom", "hall", "children")},
    "kitchen": {"name": "Кухонный гарнитур", "say": ("кухонн", "гарнитур", "kitchen cabinet", "шкаф кухон", "кухню"),
                "size": (2400, 600, 900), "place": "wall", "material": "wood_white", "rooms": ("kitchen", "kitchen_living")},
    "stove": {"name": "Плита", "say": ("плит(?:а|у|ой|ы|е)(?![а-я])", "плиту", "stove", "cooker", "варочн"), "size": (600, 600, 900),
              "place": "wall", "material": "metal_steel", "rooms": ("kitchen", "kitchen_living")},
    "sink": {"name": "Раковина", "say": ("раковин", "мойк", "умывальн", "sink"), "size": (600, 480, 850),
             "place": "wall", "material": "ceramic_white", "rooms": ("bathroom", "kitchen", "toilet")},
    "toilet": {"name": "Унитаз", "say": ("унитаз", "toilet"), "size": (380, 680, 780),
               "place": "wall", "material": "ceramic_white", "rooms": ("toilet", "bathroom")},
    "bathtub": {"name": "Ванна", "say": ("ванну", "ванна", "bathtub", "bath tub"), "size": (1700, 750, 600),
                "place": "wall", "material": "ceramic_white", "rooms": ("bathroom",)},
    "shower": {"name": "Душевая кабина", "say": ("душ", "shower"), "size": (900, 900, 2100),
               "place": "corner", "material": "glass", "rooms": ("bathroom",)},
    "refrigerator": {"name": "Холодильник", "say": ("холодильн", "fridge", "refrigerator"), "size": (600, 650, 1850),
                     "place": "wall", "material": "metal_white", "rooms": ("kitchen", "kitchen_living")},
    "tv": {"name": "Телевизор", "say": ("телевизор", "тв", "tv", "television", "телик"), "size": (1250, 250, 1200),
           "place": "wall", "material": "tv_black", "rooms": ("living", "bedroom")},
    "nightstand": {"name": "Тумбочка", "say": ("тумб", "nightstand"), "size": (450, 400, 500),
                   "place": "wall", "material": "wood_oak", "rooms": ("bedroom",)},
    "washer": {"name": "Стиральная машина", "say": ("стиральн", "washer", "washing"), "size": (600, 600, 850),
               "place": "wall", "material": "metal_white", "rooms": ("bathroom", "laundry")},
}


def furniture_kind(text: str) -> str | None:
    """The furniture a phrase names («диван», «телевизор», «кухонный гарнитур»); the longest match wins."""
    n = norm(text)
    best, best_len = None, 0
    for kind, f in FURNITURE.items():
        for s in f["say"]:
            pat = s if any(c in s for c in "()?[") else re.escape(s)
            mt = re.search(r"(?<![а-яa-z])" + pat, n)
            if mt and len(mt.group(0)) > best_len:
                best, best_len = kind, len(mt.group(0))
    return best


def nominative(name: str) -> str:
    """«детскую» → «Детская», «гостевую спальню» → «Гостевая спальня» (what a renamed room is called)."""
    words = []
    for w in str(name).strip().split():
        lw = w.casefold()
        for a, b in (("ую", "ая"), ("юю", "яя"), ("ю", "я")):
            if lw.endswith(a) and len(lw) > 4:
                lw = lw[: -len(a)] + b
                break
        words.append(lw)
    out = " ".join(words)
    return out[:1].upper() + out[1:]


# ---------------------------------------------------------------- materials
# id: name, colour (linear-ish sRGB 0..1), roughness, procedural pattern for Blender, words that name it
MATERIALS: dict[str, dict] = {
    "plaster_white": {"name": "Белая штукатурка", "color": (0.92, 0.91, 0.88), "rough": 0.85, "pattern": "plain",
                      "say": ("бел", "white")},
    "plaster_gray": {"name": "Серая штукатурка", "color": (0.62, 0.62, 0.62), "rough": 0.85, "pattern": "plain",
                     "say": ("сер", "gray", "grey")},
    "plaster_beige": {"name": "Бежевая штукатурка", "color": (0.86, 0.78, 0.64), "rough": 0.85, "pattern": "plain",
                      "say": ("беж", "beige", "песочн")},
    "paint_blue": {"name": "Голубая краска", "color": (0.55, 0.70, 0.86), "rough": 0.7, "pattern": "plain",
                   "say": ("голуб", "син", "blue")},
    "paint_green": {"name": "Зелёная краска", "color": (0.56, 0.72, 0.56), "rough": 0.7, "pattern": "plain",
                    "say": ("зелен", "green", "оливк")},
    "paint_yellow": {"name": "Жёлтая краска", "color": (0.95, 0.85, 0.45), "rough": 0.7, "pattern": "plain",
                     "say": ("желт", "yellow")},
    "paint_black": {"name": "Чёрная краска", "color": (0.08, 0.08, 0.08), "rough": 0.6, "pattern": "plain",
                    "say": ("черн", "black", "графит")},
    "brick_red": {"name": "Красный кирпич", "color": (0.55, 0.25, 0.18), "rough": 0.9, "pattern": "brick",
                  "say": ("кирпич", "brick")},
    "concrete": {"name": "Бетон", "color": (0.58, 0.58, 0.56), "rough": 0.95, "pattern": "noise",
                 "say": ("бетон", "concrete", "лофт")},
    "oak_light": {"name": "Светлый дуб", "color": (0.78, 0.62, 0.42), "rough": 0.6, "pattern": "wood",
                  "say": ("светл\\w* дуб", "дуб", "oak", "ламинат", "паркет", "дерев")},
    "oak_dark": {"name": "Тёмный дуб", "color": (0.32, 0.20, 0.11), "rough": 0.55, "pattern": "wood",
                 "say": ("темн\\w* дуб", "dark oak", "венге", "темн\\w* дерев")},
    "walnut": {"name": "Орех", "color": (0.42, 0.28, 0.17), "rough": 0.55, "pattern": "wood",
               "say": ("орех", "walnut")},
    "tile_white": {"name": "Белая плитка", "color": (0.93, 0.93, 0.93), "rough": 0.3, "pattern": "tile",
                   "say": ("бел\\w* плитк", "плитк", "кафел", "tile")},
    "tile_gray": {"name": "Серая плитка", "color": (0.55, 0.56, 0.58), "rough": 0.35, "pattern": "tile",
                  "say": ("сер\\w* плитк", "керамогранит")},
    "marble": {"name": "Мрамор", "color": (0.90, 0.89, 0.87), "rough": 0.15, "pattern": "noise",
               "say": ("мрамор", "marble")},
    "carpet_gray": {"name": "Серый ковролин", "color": (0.45, 0.45, 0.47), "rough": 1.0, "pattern": "noise",
                    "say": ("ковролин", "ковер", "carpet")},
    # furniture and openings
    "fabric_gray": {"name": "Серая ткань", "color": (0.40, 0.41, 0.43), "rough": 0.95, "pattern": "plain", "say": ()},
    "fabric_white": {"name": "Светлая ткань", "color": (0.88, 0.87, 0.84), "rough": 0.95, "pattern": "plain", "say": ()},
    "fabric_blue": {"name": "Синяя ткань", "color": (0.20, 0.30, 0.50), "rough": 0.95, "pattern": "plain",
                    "say": ("син\\w* ткан", "синий диван")},
    "wood_oak": {"name": "Дуб (мебель)", "color": (0.66, 0.50, 0.32), "rough": 0.55, "pattern": "wood", "say": ()},
    "wood_white": {"name": "Белый фасад", "color": (0.93, 0.93, 0.91), "rough": 0.4, "pattern": "plain", "say": ()},
    "metal_steel": {"name": "Сталь", "color": (0.62, 0.63, 0.65), "rough": 0.3, "pattern": "plain", "metal": 1.0,
                    "say": ("стал", "металл", "steel")},
    "metal_white": {"name": "Белый металл", "color": (0.92, 0.92, 0.92), "rough": 0.3, "pattern": "plain", "say": ()},
    "ceramic_white": {"name": "Белая керамика", "color": (0.95, 0.95, 0.95), "rough": 0.15, "pattern": "plain",
                      "say": ()},
    "glass": {"name": "Стекло", "color": (0.75, 0.85, 0.90), "rough": 0.05, "pattern": "plain", "alpha": 0.3,
              "say": ("стекл", "glass")},
    "tv_black": {"name": "Чёрный глянец", "color": (0.03, 0.03, 0.03), "rough": 0.2, "pattern": "plain", "say": ()},
    "frame_white": {"name": "Белый профиль", "color": (0.95, 0.95, 0.95), "rough": 0.4, "pattern": "plain", "say": ()},
    "door_wood": {"name": "Дверное полотно", "color": (0.70, 0.55, 0.38), "rough": 0.5, "pattern": "wood", "say": ()},
    "slab_concrete": {"name": "Плита перекрытия", "color": (0.70, 0.70, 0.68), "rough": 0.95, "pattern": "plain",
                      "say": ()},
    "roof_dark": {"name": "Кровля", "color": (0.25, 0.26, 0.28), "rough": 0.8, "pattern": "plain", "say": ()},
    "stair_wood": {"name": "Лестница (дерево)", "color": (0.60, 0.45, 0.30), "rough": 0.55, "pattern": "wood", "say": ()},
    "column_concrete": {"name": "Колонна", "color": (0.80, 0.80, 0.78), "rough": 0.9, "pattern": "plain", "say": ()},
}

DEFAULT_MATERIALS = {"wall": "plaster_white", "floor": "oak_light", "ceiling": "plaster_white", "slab": "slab_concrete",
                     "roof": "roof_dark", "door": "door_wood", "window": "frame_white", "stair": "stair_wood",
                     "column": "column_concrete", "floor_wet": "tile_white"}


def material_of(text: str) -> str | None:
    """The material a phrase names («тёмным дубом» → oak_dark, «белыми» → plaster_white); the longest match wins."""
    n = norm(text)
    best, best_len = None, 0
    for mid, m in MATERIALS.items():
        for s in m["say"]:
            mt = re.search(r"(?<![а-яa-z])" + s, n)
            if mt and len(mt.group(0)) > best_len:
                best, best_len = mid, len(mt.group(0))
    return best
