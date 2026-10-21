"""Deterministic fast path: arithmetic, percentages and unit conversions.

Answers in a short, spoken-friendly form. Evaluation uses a tiny AST walker
(numbers and + - * / ** only) — never eval()."""
from __future__ import annotations

import ast
import math
import operator
import re

WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "три": 3, "трёх": 3, "трех": 3,
    "четыре": 4, "четырёх": 4, "пять": 5, "пяти": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "двадцати": 20, "тридцать": 30,
    "тридцати": 30, "сорок": 40, "пятьдесят": 50, "пятидесяти": 50, "шестьдесят": 60, "семьдесят": 70,
    "восемьдесят": 80, "девяносто": 90, "сто": 100, "ста": 100, "двести": 200, "двухсот": 200, "триста": 300,
    "трёхсот": 300, "трехсот": 300, "четыреста": 400, "четырёхсот": 400, "пятьсот": 500, "пятисот": 500,
    "шестьсот": 600, "шестисот": 600, "семьсот": 700, "семисот": 700, "восемьсот": 800, "восьмисот": 800,
    "девятьсот": 900, "девятисот": 900, "тысяча": 1000, "тысячи": 1000, "тысяч": 1000,
}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}

# unit -> (kind, factor to base)
UNITS = {
    "миллиметр": ("len", 0.001), "мм": ("len", 0.001), "сантиметр": ("len", 0.01), "см": ("len", 0.01),
    "дециметр": ("len", 0.1), "метр": ("len", 1.0), "километр": ("len", 1000.0), "км": ("len", 1000.0),
    "дюйм": ("len", 0.0254), "фут": ("len", 0.3048), "миля": ("len", 1609.344), "мил": ("len", 1609.344),
    "миллиграмм": ("mass", 0.001), "грамм": ("mass", 1.0), "килограмм": ("mass", 1000.0), "кг": ("mass", 1000.0),
    "центнер": ("mass", 100000.0), "тонн": ("mass", 1_000_000.0), "фунт": ("mass", 453.59237),
    "миллилитр": ("vol", 0.001), "литр": ("vol", 1.0), "кубометр": ("vol", 1000.0),
    "секунд": ("time", 1.0), "минут": ("time", 60.0), "час": ("time", 3600.0), "сутк": ("time", 86400.0),
    "день": ("time", 86400.0), "дня": ("time", 86400.0), "дней": ("time", 86400.0), "недел": ("time", 604800.0),
    "квадратн метр": ("area", 1.0), "квадратн сантиметр": ("area", 0.0001), "гектар": ("area", 10000.0),
    "сотк": ("area", 100.0), "ар": ("area", 100.0),
}


def num_words(text: str) -> str:
    """'двадцать пять процентов от восьмисот' -> '25 процентов от 800'."""
    tokens = re.split(r"(\s+)", text)
    out, acc = [], None
    for tok in tokens:
        w = tok.casefold().replace("ё", "е") if tok.strip() else tok
        val = WORDS.get(w) if tok.strip() else None
        if val is None and tok.strip():
            val = WORDS.get(tok.casefold())
        if val is not None:
            if val == 1000 and acc:
                acc *= 1000
            else:
                acc = (acc or 0) + val
            continue
        if not tok.strip() and acc is not None:
            continue
        if acc is not None:
            out.append(str(acc) + " ")
            acc = None
        out.append(tok)
    if acc is not None:
        out.append(str(acc))
    return "".join(out)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 100:
            raise ValueError("слишком большая степень")
        return OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "sqrt" and len(node.args) == 1:
        return math.sqrt(_eval(node.args[0]))
    raise ValueError("недопустимое выражение")


def fmt(x: float) -> str:
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    if isinstance(x, int):
        return f"{x:,}".replace(",", " ")
    s = f"{x:.6g}" if abs(x) < 1e6 else f"{x:,.2f}".replace(",", " ")
    return s.replace(".", ",")


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


NUM = r"(\d+(?:[.,]\d+)?)"


def answer(text: str) -> str | None:
    """Return a spoken-friendly answer, or None if this is not a calculation."""
    t = num_words(text.casefold().replace("ё", "е"))
    t = re.sub(r"(?<=\d)\s(?=\d{3}\b)", "", t)          # "1 000" -> "1000"
    m = re.search(rf"{NUM}\s*(?:%|процент\w*)\s*(?:от|из)\s*{NUM}", t)
    if m:
        p, base = _num(m.group(1)), _num(m.group(2))
        v = p * base / 100
        return f"{fmt(v)}. {fmt(p)} процентов от {fmt(base)} — это {fmt(v)}."
    m = re.search(rf"сколько\s+(?:будет\s+)?(\w+(?:\s\w+)?)\s+в\s+(?:{NUM}\s+|одн\w+\s+)?(\w+(?:\s\w+)?)", t)
    if m:
        ua, ub = _unit(m.group(1)), _unit(m.group(3))
        n = _num(m.group(2)) if m.group(2) else 1
        if ua and ub and ua[0] == ub[0]:
            v = n * ub[1] / ua[1]
            return f"{fmt(v)}. В {fmt(n) + ' ' if n != 1 else 'одном '}{m.group(3)} — {fmt(v)} {m.group(1)}."
    m = re.search(rf"корень\s+(?:квадратный\s+)?из\s+{NUM}", t)
    if m and not re.search(r"[+\-*/]|плюс|минус|умнож|раздел", t):
        n = _num(m.group(1))
        return f"{fmt(math.sqrt(n))}. Квадратный корень из {fmt(n)} — {fmt(math.sqrt(n))}."
    m = re.search(rf"(?:сколько\s+будет|посчитай|вычисли|calculate|сколько\s+это)\s+(.+?)\??$", t) or \
        re.fullmatch(r"\s*([\d\s.,+\-*/x×÷^()]+(?:в квадрате|в кубе)?)\s*(?:=|равно)?\s*\??\s*", t)
    if m:
        expr = m.group(1)
        expr = re.sub(r"корень\s+(?:квадратный\s+)?из\s+" + NUM, r"sqrt(\1)", expr)
        repl = [(r"\bплюс\b", "+"), (r"\bминус\b", "-"), (r"(?:разделить|поделить|делить)\s+на", "/"),
                (r"умножить\s+на|\bумножить\b|\bна\b(?=\s*\d)", "*"), (r"\bв квадрате\b", "**2"), (r"\bв кубе\b", "**3"),
                (r"\bв степени\b", "**"), (r"[x×]", "*"), (r"÷", "/"), (r"\^", "**"), (r",(?=\d)", ".")]
        for a, b in repl:
            expr = re.sub(a, b, expr)
        expr = expr.strip(" ?.!")
        if not re.fullmatch(r"[\d\s.+\-*/()sqrt]+", expr) or not re.search(r"\d", expr) or \
                not re.search(r"[+\-*/]|sqrt", expr):
            return None
        try:
            v = _eval(ast.parse(expr, mode="eval"))
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
            return None
        return f"{fmt(v)}. {spoken(expr)} — {fmt(v)}."
    return None


def _unit(word: str):
    w = word.casefold()
    for key in sorted(UNITS, key=len, reverse=True):
        if all(part in w for part in key.split()):
            return UNITS[key]
    return None


def spoken(expr: str) -> str:
    """'15*3' -> '15 умножить на 3' (reads naturally in speech and on screen)."""
    e = re.sub(r"\s+", "", expr)
    e = re.sub(r"sqrt\(([^)]*)\)", r" корень из \1 ", e)
    for a, b in (("**2", " в квадрате"), ("**3", " в кубе"), ("**", " в степени "), ("*", " умножить на "),
                 ("/", " разделить на "), ("+", " плюс "), ("-", " минус ")):
        e = e.replace(a, b)
    e = re.sub(r"\d+(?:\.\d+)?", lambda m: fmt(float(m.group(0))), e)
    e = re.sub(r"\s+", " ", e).strip()
    return e[:1].upper() + e[1:]
