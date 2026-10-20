"""Deterministic analysis of project mail and documents.

Every extracted fact carries the exact source quote and location, so the UI can
show where a number, date or size came from. Nothing here calls a model.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

# ---------------------------------------------------------------- classification
INJECTION = [
    r"игнорируй\s+(все\s+)?(предыдущие\s+|прошлые\s+)?(инструкции|указания)",
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"(перешли|отправь|forward|send)\s+все\s+(сметы|файлы|документы|договоры)",
    r"подтверждение\s+не\s+требуется",
    r"джарвис,?\s+(сделай|выполни|перешли|удали)",
]
PROMO = [r"скидк", r"распродаж", r"только сегодня", r"акци[яи]", r"unsubscribe", r"отписаться", r"дайджест"]
DECISION = [r"прошу\s+подтвердить", r"нужно\s+(ваше\s+)?решение", r"подтвердите", r"прошу\s+согласовать",
            r"согласуйте", r"нужен\s+ответ", r"ответ\s+нужен"]
PROMISE = [r"гарантируем", r"обязуемся", r"отгрузим", r"поставим", r"доставим"]

TYPE_RANK = {"DECISION_REQUIRED": 0, "QUESTION": 1, "PROMISE": 2, "FYI": 3}


def _find(patterns: list[str], text: str) -> re.Match | None:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m
    return None


def evidence(msg: dict, field: str, m_start: int, m_end: int, pad: int = 60) -> dict:
    text = msg[field]
    a, b = max(0, m_start - pad), min(len(text), m_end + pad)
    return {"kind": "email", "id": msg["id"], "field": field, "start": m_start, "end": m_end,
            "quote": text[m_start:m_end], "context": ("…" if a else "") + text[a:b] + ("…" if b < len(text) else ""),
            "label": f"Письмо: {msg['from']['name']}, {msg['date'][:10]}"}


def classify(msg: dict, project: dict) -> dict:
    text = msg["subject"] + "\n" + msg["body"]
    low = text.casefold()
    sender = msg["from"]["email"].casefold()
    contacts = {c["email"].casefold(): c for c in project["contacts"]}
    reasons: list[str] = []
    alias = next((a for a in project["aliases"] if a in low), None)
    in_project = bool(alias) or sender in contacts
    inj = _find(INJECTION, text)
    if inj:
        reasons.append(f"Текст похож на команду для ассистента: «{inj.group(0)}» — письмо изолировано, ничего не выполнялось")
        if sender not in contacts:
            reasons.append("Отправитель не числится среди контактов проекта")
        return {"category": "SUSPICIOUS", "type": "FYI", "reasons": reasons, "project": in_project}
    if "list-unsubscribe" in {k.casefold() for k in msg.get("headers", {})} or (
            _find(PROMO, text) and sender not in contacts):
        reasons.append("Рассылка: есть заголовок отписки или рекламные признаки"
                       + ("; слово «срочно» в теме не делает письмо важным" if "срочно" in low else ""))
        return {"category": "MARKETING", "type": "FYI", "reasons": reasons, "project": False}
    if not in_project:
        reasons.append("Не относится к проекту «Флора»")
        return {"category": "OTHER_PROJECT", "type": "FYI", "reasons": reasons, "project": False}
    if alias:
        reasons.append(f"Упоминает проект («{alias}»)")
    if sender in contacts:
        c = contacts[sender]
        reasons.append(f"Отправитель — {c['role']} проекта ({c['org']})")
    if _find(DECISION, text):
        typ = "DECISION_REQUIRED"
    elif _find(PROMISE, text):
        typ = "PROMISE"
    elif "?" in msg["body"]:
        typ = "QUESTION"
    else:
        typ = "FYI"
    return {"category": "PROJECT", "type": typ, "reasons": reasons, "project": True}


# ---------------------------------------------------------------- fact extraction
DATE_RE = re.compile(r"(?:до\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
MONEY_RE = re.compile(r"(\d[\d\s]{0,12}\d|\d)\s*(тыс\.?\s*)?(руб\.?|₽|EUR|€)", re.I)
DIM_RE = re.compile(r"(?:(W-\d+P?)\D{0,25}?)?(\d{3,4})\s*[×xх]\s*(\d{3,4})")
CODE_RE = re.compile(r"\bW-\d+P?\b")


def _year_for(day: int, month: int, sent: dt.date) -> int:
    y = sent.year
    return y + 1 if month < sent.month - 6 else y


def extract_facts(msg: dict) -> list[dict]:
    facts: list[dict] = []
    body = msg["body"]
    sent = dt.date.fromisoformat(msg["date"][:10])
    for m in DATE_RE.finditer(body):
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        y = int(m.group(3)) if m.group(3) else _year_for(d, mo, sent)
        y = y + 2000 if y < 100 else y
        try:
            date = dt.date(y, mo, d)
        except ValueError:
            continue
        before = body[max(0, m.start() - 40):m.start()].casefold()
        kind = "deadline" if m.group(0).startswith("до") or "до" in before[-6:] else "date"
        if re.search(r"отгруз|поставк|доставк", body[max(0, m.start() - 30):m.end() + 5], re.I):
            kind = "delivery"
        facts.append({"kind": kind, "value": date.isoformat(), "text": m.group(0),
                      "evidence": evidence(msg, "body", m.start(), m.end())})
    for m in MONEY_RE.finditer(body):
        num = int(re.sub(r"\s", "", m.group(1)))
        if m.group(2):
            num *= 1000
        cur = "RUB" if m.group(3).lower().startswith(("руб", "₽")) else "EUR"
        approx = bool(re.search(r"около|примерно|~", body[max(0, m.start() - 12):m.start()], re.I))
        facts.append({"kind": "money", "value": num, "currency": cur, "approx": approx, "text": m.group(0),
                      "evidence": evidence(msg, "body", m.start(), m.end())})
    for m in DIM_RE.finditer(body):
        code = m.group(1)
        if not code:
            near = CODE_RE.findall(body[max(0, m.start() - 40):m.start()])
            code = near[-1] if near else None
        facts.append({"kind": "dimension", "element": code, "width": int(m.group(2)), "height": int(m.group(3)),
                      "text": m.group(0), "evidence": evidence(msg, "body", m.start(2), m.end())})
    return facts


# ---------------------------------------------------------------- spreadsheets
_MUL = re.compile(r"^=C(\d+)\*E(\d+)$")
_SUM = re.compile(r"^=SUM\(F(\d+):F(\d+)\)$", re.I)


@dataclass
class Cell:
    ref: str
    value: object


def read_quote(path: Path) -> dict:
    """Read a supplier quote. Formulas are evaluated only for the patterns we can
    compute exactly; anything else is reported as not evaluated (never guessed)."""
    wb = load_workbook(path, data_only=False)
    ws = wb.active
    title = ws["A1"].value or path.stem
    meta = ws["A2"].value or ""
    currency = "EUR" if "EUR" in (ws["E4"].value or "") else "RUB"
    lines: list[dict] = []
    values: dict[int, float] = {}
    total = None
    notes: list[str] = []
    anomalies: list[dict] = []
    r = 5
    while r < ws.max_row + 1:
        code = ws.cell(r, 1).value
        label = ws.cell(r, 5).value
        f = ws.cell(r, 6).value
        if label == "Итого":
            computed = None
            m = _SUM.match(str(f or ""))
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                computed = sum(values.get(i, 0) for i in range(a, b + 1))
                covered = set(range(a, b + 1))
                missing = [ln for ln in lines if ln["row"] not in covered]
                if missing:
                    anomalies.append({
                        "kind": "total_formula_range",
                        "text": "Итоговая формула не включает строки: " + ", ".join(
                            f"{ln['code']} ({ln['name']}, {fmt_money(ln['sum'], currency)})" for ln in missing),
                        "cell": f"F{r}", "formula": f, "missing_sum": sum(ln["sum"] for ln in missing)})
            total = {"cell": f"F{r}", "formula": f, "value": computed}
            r += 1
            continue
        if code and isinstance(ws.cell(r, 3).value, (int, float)):
            qty, price = ws.cell(r, 3).value, ws.cell(r, 5).value
            m = _MUL.match(str(f or ""))
            s = qty * price if m and int(m.group(1)) == r == int(m.group(2)) else None
            if s is None:
                anomalies.append({"kind": "formula_unknown", "text": f"Формула в F{r} не вычислена: {f}",
                                  "cell": f"F{r}"})
            values[r] = s or 0
            dims = DIM_RE.search(ws.cell(r, 2).value or "")
            lines.append({"row": r, "code": code, "name": ws.cell(r, 2).value, "qty": qty,
                          "unit": ws.cell(r, 4).value, "price": price, "sum": s, "formula": f,
                          "cells": {"price": f"E{r}", "sum": f"F{r}"},
                          "dims": {"width": int(dims.group(2)), "height": int(dims.group(3))} if dims else None})
        elif ws.cell(r, 1).value and r > 5:
            notes.append(str(ws.cell(r, 1).value))
        r += 1
    meta_low = meta.casefold()
    notes_low = " ".join(notes).casefold()
    vat = "included" if "с ндс" in meta_low else "excluded" if "без ндс" in meta_low else "unknown"
    install = any(ln["code"] == "MNT" for ln in lines) and "монтаж не включ" not in meta_low + notes_low
    return {"file": path.name, "title": title, "meta": meta, "currency": currency, "vat": vat,
            "installation_included": install, "lines": lines, "total": total, "notes": notes,
            "sum_of_lines": sum(ln["sum"] or 0 for ln in lines), "anomalies": anomalies}


def fmt_money(v: float | None, currency: str = "RUB") -> str:
    if v is None:
        return "—"
    s = f"{v:,.0f}".replace(",", " ")
    return f"{s} ₽" if currency == "RUB" else f"{s} €"


def compare_quotes(base: dict, new: dict) -> dict:
    """Line-by-line comparison of two versions from the same supplier."""
    bmap = {ln["code"]: ln for ln in base["lines"]}
    nmap = {ln["code"]: ln for ln in new["lines"]}
    rows = []
    used_new = set()
    for code, b in bmap.items():
        n = nmap.get(code)
        if n is None:   # replaced by a variant code, e.g. W-3 -> W-3P
            n = next((x for c, x in nmap.items() if c not in bmap and c.startswith(code)), None)
        if n is None:
            rows.append({"code": code, "status": "removed", "base": b, "new": None, "delta": -(b["sum"] or 0)})
            continue
        used_new.add(n["code"])
        status = "replaced" if n["code"] != code else ("same" if n["sum"] == b["sum"] and n["name"] == b["name"]
                                                      else "changed")
        rows.append({"code": code if n["code"] == code else f"{code} → {n['code']}", "status": status, "base": b,
                     "new": n, "delta": (n["sum"] or 0) - (b["sum"] or 0)})
    for code, n in nmap.items():
        if code not in bmap and code not in used_new:
            rows.append({"code": code, "status": "added", "base": None, "new": n, "delta": n["sum"] or 0})
    delta = new["sum_of_lines"] - base["sum_of_lines"]
    file_delta = None
    if new["total"] and new["total"]["value"] is not None and base["total"] and base["total"]["value"] is not None:
        file_delta = new["total"]["value"] - base["total"]["value"]
    return {"base_file": base["file"], "new_file": new["file"], "currency": new["currency"], "rows": rows,
            "base_total": base["sum_of_lines"], "new_total": new["sum_of_lines"], "delta": delta,
            "file_delta": file_delta, "anomalies": new["anomalies"] + base["anomalies"],
            "same_basis": base["currency"] == new["currency"] and base["vat"] == new["vat"]}


def comparability(a: dict, b: dict) -> dict:
    reasons = []
    if a["currency"] != b["currency"]:
        reasons.append(f"Разные валюты ({a['currency']} и {b['currency']}); курса и даты пересчёта в документах нет")
    if a["vat"] != b["vat"]:
        reasons.append("Разный учёт НДС: " + {"included": "с НДС", "excluded": "без НДС", "unknown": "не указан"}[a["vat"]]
                       + " и " + {"included": "с НДС", "excluded": "без НДС", "unknown": "не указан"}[b["vat"]])
    if a["installation_included"] != b["installation_included"]:
        reasons.append("Монтаж включён только в одно предложение")
    return {"comparable": not reasons, "reasons": reasons}


# ---------------------------------------------------------------- drawings
LABEL_RE = re.compile(r"<text[^>]*>(W-\d+P?) · (\d{3,4})×(\d{3,4})</text>")
REV_RE = re.compile(r"Ревизия ([A-Z]) · ([\d.]+)")


def read_drawing(path: Path) -> dict:
    svg = path.read_text("utf-8")
    rev = REV_RE.search(svg)
    elements = [{"element": m.group(1), "width": int(m.group(2)), "height": int(m.group(3)),
                 "quote": f"{m.group(1)} · {m.group(2)}×{m.group(3)}"}
                for m in LABEL_RE.finditer(svg)]
    status = "approved" if "Согласовано" in svg else "proposal" if "не согласовано" in svg else "unknown"
    return {"file": path.name, "revision": rev.group(1) if rev else None, "date": rev.group(2) if rev else None,
            "status": status, "elements": elements}


def find_contradictions(dim_facts: list[dict]) -> list[dict]:
    """Group size statements by element; report elements whose stated sizes differ."""
    by_el: dict[str, list[dict]] = {}
    for f in dim_facts:
        if f.get("element"):
            by_el.setdefault(f["element"], []).append(f)
    out = []
    for el, fs in by_el.items():
        for attr in ("width", "height"):
            vals = {f[attr] for f in fs}
            if len(vals) > 1:
                out.append({"element": el, "attribute": attr,
                            "attribute_ru": "ширина" if attr == "width" else "высота",
                            "values": [{"value": f[attr], "source": f["source"]} for f in fs],
                            "status": "UNRESOLVED"})
    return out
