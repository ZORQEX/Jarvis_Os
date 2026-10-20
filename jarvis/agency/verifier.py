"""Verification of AI step outputs against their inputs.

Model output is a claim, not a fact. Numbers in generated text must be
traceable to the data the step received; disagreements are resolved by the
original source, never by whichever model sounds more certain."""
from __future__ import annotations

import re

NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d+))?(?![\w])")


def numbers(text: str, min_value: float = 10) -> set[str]:
    out = set()
    for m in NUM.finditer(text or ""):
        whole = re.sub(r"[   ]", "", m.group(1))
        val = whole + ("." + m.group(2) if m.group(2) else "")
        try:
            if float(val) < min_value:
                continue
        except ValueError:
            continue
        out.add(val.rstrip("0").rstrip(".") if "." in val else val)
    return out


def ungrounded_numbers(output: str, inputs: list[str]) -> list[str]:
    """Numbers (≥10) present in the output but in none of the inputs."""
    have = set()
    for t in inputs:
        have |= numbers(t, 0)
    return sorted(n for n in numbers(output) if n not in have)


def reconcile(claims: dict[str, str], source_text: str) -> dict:
    """claims: {provider: value_text}. The source text decides; unresolved if it supports none or several."""
    src = numbers(source_text, 0)
    supported = {p: v for p, v in claims.items() if numbers(v, 0) & src}
    values = {p: sorted(numbers(v, 0)) for p, v in claims.items()}
    distinct = {tuple(v) for v in values.values()}
    if len(distinct) <= 1:
        return {"status": "agree", "values": values}
    if len(supported) == 1:
        p = next(iter(supported))
        return {"status": "resolved_by_source", "verified": values[p], "provider": p, "values": values}
    return {"status": "unresolved", "values": values, "note": "источник не подтверждает однозначно ни одно значение"}


def verify_step(action: str, text: str, sources: list[dict], inputs: list[str]) -> tuple[str, str]:
    if not (text or "").strip():
        return "failed", "Пустой результат"
    if action == "research":
        if sources:
            return "verified", f"Есть источники: {len(sources)} (содержание источников не перепроверялось)"
        return "unverified", "Источники не получены — факты не подтверждены"
    if action in ("write", "translate", "analyze"):
        bad = ungrounded_numbers(text, inputs)
        if bad:
            return "unverified", "Числа без подтверждения во входных данных: " + ", ".join(bad[:8])
        return "verified", "Все числа в тексте есть во входных данных; текст не отправлялся"
    return "unverified", "Автоматическая проверка для этого шага не предусмотрена"
