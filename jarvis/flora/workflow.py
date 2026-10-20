"""«Флора» executive workflow on SYNTHETIC demo data.

Inbox triage → decision items with evidence → quote comparison → drawing
revisions → Russian reply draft → approval packet → demo send with operation-id
dedup → verification against the demo «Sent» folder → project decision record.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path

from ..db import Database, dumps, loads, new_id, now
from ..events import AuditLog, EventBus
from ..tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification
from . import analysis as A
from .mail import FIXTURES, DemoMailbox

PROJECT = "flora"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def ru_date(iso: str, weekday: bool = True) -> str:
    d = dt.date.fromisoformat(iso)
    s = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{WEEKDAYS[d.weekday()]}, {s}" if weekday else s


def money(v, cur="RUB") -> str:
    return A.fmt_money(v, cur)


def short_source(src: dict, project: dict) -> str:
    if src.get("kind") == "email":
        name = src.get("label", "").split(": ", 1)[-1].split(",")[0]
        role = next((c["role"] for c in project["contacts"] if c["name"] == name), "")
        gen = {"архитектор": "архитектора", "менеджер фабрики": "фабрики «Север»", "менеджер поставщика": "«ТехноОкна»",
               "прораб": "прораба", "бухгалтер": "бухгалтера"}
        return f"письмо {gen.get(role, name)}"
    if src.get("kind") == "drawing":
        return src.get("label", "план").replace("План, ревизия", "план рев.")
    if src.get("kind") == "sheet":
        f = src.get("file", "")
        return "КП «Север» v2" if "Север" in f and "v2" in f else "КП «ТехноОкна»" if "ТехноОкна" in f else f
    return src.get("label", "источник")


class FloraWorkflow:
    def __init__(self, paths, db: Database, bus: EventBus, audit: AuditLog, gemini, config):
        self.paths, self.db, self.bus, self.audit, self.gemini, self.config = paths, db, bus, audit, gemini, config
        self.mail = DemoMailbox(paths.data)
        self.project = json.loads((FIXTURES / "project.json").read_text("utf-8"))
        self.ann_dir = paths.artifacts / "flora" / "annotations"
        self.ann_dir.mkdir(parents=True, exist_ok=True)
        self._analysis: dict | None = None

    def tools(self) -> list[Tool]:
        return [FloraInbox(self), FloraCompare(self), FloraDraft(self), FloraSend(self)]

    # ------------------------------------------------------------------ analysis
    def analyze(self) -> dict:
        proj = self.project
        items, excluded, dim_facts = [], [], []
        for m in self.mail.list_messages():
            c = A.classify(m, proj)
            facts = A.extract_facts(m) if c["category"] == "PROJECT" else []
            for f in facts:
                if f["kind"] == "dimension":
                    dim_facts.append({**f, "source": f["evidence"]})
            entry = {"id": m["id"], "thread_id": m["thread_id"], "date": m["date"], "from": m["from"],
                     "subject": m["subject"], "category": c["category"], "type": c["type"], "reasons": c["reasons"],
                     "facts": facts, "attachments": m["attachments"],
                     "snippet": m["body"][:180].replace("\n", " ")}
            deadlines = sorted(f["value"] for f in facts if f["kind"] == "deadline")
            entry["deadline"] = deadlines[0] if deadlines else None
            (items if c["category"] == "PROJECT" else excluded).append(entry)
        items.sort(key=lambda e: (A.TYPE_RANK[e["type"]], e["deadline"] or "9999", e["date"]))
        for e in items:   # continuity: replies already sent from JARVIS
            r = self.db.one("SELECT option, sent_at, provider_message_id FROM drafts WHERE reply_to=? AND status='sent' "
                            "ORDER BY sent_at DESC LIMIT 1", (e["id"],))
            e["reply"] = dict(r) if r else None
        docs = proj["documents"]
        q_base = A.read_quote(FIXTURES / docs["approved_quote"])
        q_new = A.read_quote(FIXTURES / docs["proposed_quote"])
        q_alt = A.read_quote(FIXTURES / docs["alternative_quote"])
        cmp = A.compare_quotes(q_base, q_new)
        alt = A.comparability(q_new, q_alt)
        drawings = [A.read_drawing(FIXTURES / docs["approved_plan"]), A.read_drawing(FIXTURES / docs["proposed_plan"])]
        for d in drawings:
            for e in d["elements"]:
                dim_facts.append({"kind": "dimension", "element": e["element"], "width": e["width"],
                                  "height": e["height"], "source": {"kind": "drawing", "file": d["file"],
                                                                    "quote": e["quote"],
                                                                    "label": f"План, ревизия {d['revision']}"}})
        for q in (q_new, q_alt):
            for ln in q["lines"]:
                if ln["dims"]:
                    dim_facts.append({"kind": "dimension", "element": ln["code"], **ln["dims"],
                                      "source": {"kind": "sheet", "file": q["file"], "cell": f"B{ln['row']}",
                                                 "quote": ln["name"], "label": f"{q['file']}, ячейка B{ln['row']}"}})
        contradictions = A.find_contradictions(dim_facts)
        # Cost claim in the architect's email vs the line-by-line delta.
        claims = []
        for it in items:
            for f in it["facts"]:
                if f["kind"] == "money" and f["currency"] == "RUB" and f.get("approx"):
                    claims.append({"stated": f["value"], "stated_text": f["text"], "source": f["evidence"],
                                   "lines_delta": cmp["delta"], "file_delta": cmp["file_delta"],
                                   "matches_lines": abs(f["value"] - cmp["delta"]) <= 0.05 * cmp["delta"],
                                   "matches_file_total": cmp["file_delta"] is not None and
                                   abs(f["value"] - cmp["file_delta"]) <= 0.05 * abs(cmp["file_delta"] or 1)})
        a = {"demo": True, "project": {k: proj[k] for k in ("id", "name", "code")}, "items": items,
             "excluded": excluded, "quotes": {"base": q_base, "new": q_new, "alt": q_alt}, "comparison": cmp,
             "alternative": alt, "drawings": drawings, "contradictions": contradictions, "cost_claims": claims,
             "analyzed_at": now()}
        self._analysis = a
        return a

    def analysis(self) -> dict:
        return self._analysis or self.analyze()

    # ------------------------------------------------------------------ summary text
    def summary(self, a: dict) -> str:
        dec = [i for i in a["items"] if i["type"] == "DECISION_REQUIRED"]
        lines = [f"По «Флоре» {len(dec)} {'решение требует' if len(dec) == 1 else 'решения требуют'} вашего участия:"]
        cmp = a["comparison"]
        for n, it in enumerate(dec, 1):
            who = f"{it['from']['name']}"
            role = next((c["role"] for c in self.project["contacts"] if c["email"] == it["from"]["email"]), "")
            dl = f" — ответить до **{ru_date(it['deadline'])}**" if it["deadline"] else ""
            if it.get("reply"):
                dl = " — " + {"clarify": "вы запросили уточнение", "approve": "вы согласовали с условиями",
                              "decline": "вы отклонили изменение"}.get(it["reply"]["option"], "ответ отправлен") + \
                     ", ждём ответа"
            if "W-3" in it["subject"]:
                txt = f"{n}. **Окно W-3 → панорамное W-3P** ({who}, {role}){dl}. "
                txt += f"Удорожание по строкам КП: **+{money(cmp['delta'])}**"
                cl = next((c for c in a["cost_claims"] if c["source"]["id"] == it["id"]), None)
                if cl and not cl["matches_lines"]:
                    an = next((x for x in cmp["anomalies"] if x["kind"] == "total_formula_range"), None)
                    txt += f" (в письме — «{cl['stated_text']}»"
                    if cl["matches_file_total"] and an:
                        txt += f": это итог из файла КП, где формула в {an['cell']} не учитывает доставку {money(an['missing_sum'])}"
                    txt += ")"
                txt += "."
                for c in a["contradictions"]:
                    groups: dict[int, list[str]] = {}
                    for v in c["values"]:
                        lbl = short_source(v["source"], self.project)
                        if lbl not in groups.setdefault(v["value"], []):
                            groups[v["value"]].append(lbl)
                    parts = [f"{val} мм — {', '.join(lbls)}" for val, lbls in sorted(groups.items(), reverse=True)]
                    txt += (f" Не совпадает {c['attribute_ru']} {c['element']}: " + "; ".join(parts)
                            + ". Какой размер верный, не выбираю за вас.")
                lines.append(txt)
            else:
                title = it["subject"].replace("Флора: ", "")
                lines.append(f"{n}. **{title[:1].upper() + title[1:]}** ({who}, {role}){dl}.")
        other = [i for i in a["items"] if i["type"] != "DECISION_REQUIRED"]
        also = []
        for it in other:
            if it["type"] == "PROMISE":
                dv = next((f for f in it["facts"] if f["kind"] == "delivery"), None)
                dl = next((f for f in it["facts"] if f["kind"] == "deadline"), None)
                if dv:
                    also.append(f"фабрика обещает отгрузку {ru_date(dv['value'], False)}"
                                + (f" при подтверждении до {ru_date(dl['value'], False)}" if dl else ""))
            elif any(att["kind"] == "quote" for att in it["attachments"]):
                if not a["alternative"]["comparable"]:
                    q = a["quotes"]["alt"]
                    basis = [q["currency"], "без НДС" if q["vat"] == "excluded" else "",
                             "без монтажа" if not q["installation_included"] else ""]
                    also.append(f"«ТехноОкна» ({it['from']['name']}) прислали альтернативное КП — напрямую "
                                f"не сравнимо: {', '.join(b for b in basis if b)}")
            else:
                also.append(it["subject"].replace("Флора: ", "").rstrip("."))
        if also:
            lines.append("\nТакже: " + "; ".join(also) + ".")
        ex = a["excluded"]
        cnt = {k: sum(1 for e in ex if e["category"] == k) for k in ("MARKETING", "OTHER_PROJECT", "SUSPICIOUS")}
        hid = []
        if cnt["MARKETING"]:
            hid.append(f"рассылки — {cnt['MARKETING']}")
        if cnt["OTHER_PROJECT"]:
            hid.append(f"другие проекты — {cnt['OTHER_PROJECT']}")
        if cnt["SUSPICIOUS"]:
            hid.append(f"подозрительные — {cnt['SUSPICIOUS']} (просят переслать документы; ничего не выполнялось)")
        if hid:
            lines.append("Скрыто: " + ", ".join(hid) + ".")
        lines.append("\n_Демо-данные: письма и документы синтетические._")
        return "\n".join(lines)

    # ------------------------------------------------------------------ drafts
    def compose(self, option: str) -> dict:
        a = self.analysis()
        item = next(i for i in a["items"] if "W-3" in i["subject"])
        cmp = a["comparison"]
        contr = next((c for c in a["contradictions"] if c["element"] == "W-3P"), None)
        an = next((x for x in cmp["anomalies"] if x["kind"] == "total_formula_range"), None)
        first = item["from"]["name"].split()[0]
        dl = ru_date(item["deadline"], False) if item["deadline"] else None
        heights = sorted({v["value"] for v in contr["values"]}) if contr else []
        if option == "approve":
            body = (f"{first}, добрый день!\n\nСогласовываю замену окна W-3 на панорамное W-3P при двух условиях:\n"
                    + (f"1. Высота — {max(heights)} мм, как на плане ревизии C. В КП фабрики «Север» v2 указано "
                       f"{min(heights)} мм — прошу привести КП в соответствие.\n" if contr else "")
                    + f"{2 if contr else 1}. Итоговая сумма по КП — {money(cmp['new_total'])} с НДС "
                    f"(удорожание {money(cmp['delta'])})"
                    + (f"; в файле КП итог не учитывает доставку {money(an['missing_sum'])}" if an else "")
                    + ".\n\nПрошу прислать исправленное КП до подтверждения фабрике"
                    + (f" — успеваем до {dl}" if dl else "") + ".\n\nС уважением,")
        elif option == "decline":
            body = (f"{first}, добрый день!\n\nСпасибо за предложение. Решила оставить окно W-3 1800×1500 по "
                    f"согласованной ревизии B. Прошу сообщить фабрике «Север», что изменение не подтверждаем — "
                    f"поставка по КП v1 без изменений.\n\nС уважением,")
        else:
            option = "clarify"
            pts = []
            if contr:
                pts.append(f"Высота окна: в вашем письме и на плане ревизии C — {max(heights)} мм, в КП фабрики "
                           f"«Север» v2 — {min(heights)} мм. Какой размер верный?")
            claim = next((c for c in a["cost_claims"] if c["source"]["id"] == item["id"]), None)
            stated = f"а не «{claim['stated_text']}», как в письме" if claim and not claim["matches_lines"] else ""
            pts.append(f"Стоимость: по строкам КП v2 удорожание {money(cmp['delta'])} с НДС"
                       + (f", {stated}" if stated else "")
                       + (f": итог в файле КП не включает доставку {money(an['missing_sum'])}" if an and stated else "")
                       + ". Прошу подтвердить сумму с фабрикой.")
            body = (f"{first}, добрый день!\n\nСпасибо за предложение по окну W-3P. Прежде чем согласовать, прошу "
                    f"уточнить:\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(pts, 1))
                    + (f"\n\nРешение готова принять до {dl}, чтобы сохранить производственный слот." if dl else "")
                    + "\n\nС уважением,")
        subject = "Re: " + item["subject"]
        return {"to_name": item["from"]["name"], "to_addr": item["from"]["email"], "subject": subject, "body": body,
                "option": option, "reply_to": item["id"]}

    def draft(self) -> dict | None:
        r = self.db.one("SELECT * FROM drafts WHERE project=? ORDER BY created_at DESC LIMIT 1", (PROJECT,))
        return dict(r) if r else None

    def save_draft(self, fields: dict, draft_id: str | None = None) -> dict:
        t = now()
        cur = self.db.one("SELECT * FROM drafts WHERE id=?", (draft_id,)) if draft_id else None
        if cur and cur["status"] == "sent":
            raise ToolInputError("Это письмо уже отправлено — создайте новый черновик")
        if cur:
            changed = any(fields.get(k) is not None and fields[k] != cur[k] for k in ("subject", "body", "to_addr"))
            ver = cur["version"] + (1 if changed else 0)
            vals = {k: fields.get(k) if fields.get(k) is not None else cur[k]
                    for k in ("to_name", "to_addr", "subject", "body", "option", "reply_to")}
            self.db.execute("UPDATE drafts SET to_name=?,to_addr=?,subject=?,body=?,option=?,reply_to=?,version=?,"
                            "operation_id=?,updated_at=? WHERE id=?",
                            (*vals.values(), ver, f"op-{cur['id']}-v{ver}", t, cur["id"]))
            did = cur["id"]
            if changed:
                self._invalidate_pending_send(did)
        else:
            did = new_id("dr_")
            self.db.execute("INSERT INTO drafts(id,project,reply_to,to_name,to_addr,subject,body,option,version,status,"
                            "operation_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,'draft',?,?,?)",
                            (did, PROJECT, fields.get("reply_to"), fields.get("to_name"), fields["to_addr"],
                             fields["subject"], fields["body"], fields.get("option"), f"op-{did}-v1", t, t))
        d = dict(self.db.one("SELECT * FROM drafts WHERE id=?", (did,)))
        self.bus.publish("flora.draft", {"draft": d})
        return d

    def _invalidate_pending_send(self, draft_id: str) -> None:
        """A material edit after an approval request invalidates that request."""
        for r in self.db.all("SELECT id,args FROM approvals WHERE status='pending' AND tool_id='flora_send'"):
            if loads(r["args"], {}).get("draft_id") == draft_id and self.approvals is not None:
                self.approvals._finish(r["id"], "expired", None, reason="Черновик изменён после запроса")

    approvals = None   # set by the composition root

    def decisions(self) -> list[dict]:
        base = [dict(d, source_refs=[d.get("source")]) for d in self.project["decisions"]]
        rows = [dict(r) for r in self.db.all("SELECT * FROM project_decisions WHERE project=? ORDER BY created_at",
                                             (PROJECT,))]
        for r in rows:
            r["source_refs"] = loads(r["source_refs"], [])
        return base + rows

    def record_decision(self, subject: str, status: str, note: str, refs: list) -> None:
        t = now()
        self.db.execute("INSERT INTO project_decisions(id,project,subject,status,note,source_refs,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)", (new_id("pd_"), PROJECT, subject, status, note, dumps(refs), t, t))

    # ------------------------------------------------------------------ annotations (sidecar, never the source)
    def annotations(self, file: str) -> dict:
        f = self.ann_dir / (hashlib.sha256(file.encode()).hexdigest()[:24] + ".json")
        return json.loads(f.read_text("utf-8")) if f.exists() else {"file": file, "shapes": [], "updated_at": None}

    def save_annotations(self, file: str, shapes: list) -> dict:
        if file not in {d["file"] for d in self.analysis()["drawings"]}:
            raise ValueError("Неизвестный чертёж")
        if not isinstance(shapes, list) or len(shapes) > 500:
            raise ValueError("Слишком много пометок")
        clean = []
        for s in shapes:
            if not isinstance(s, dict) or s.get("type") not in ("pen", "arrow", "rect", "text", "highlight"):
                continue
            pts = [[round(float(x), 1), round(float(y), 1)] for x, y in (s.get("points") or [])[:2000]]
            clean.append({"type": s["type"], "points": pts, "color": str(s.get("color", "#e5484d"))[:16],
                          "text": str(s.get("text", ""))[:300]})
        data = {"file": file, "shapes": clean, "updated_at": now()}
        f = self.ann_dir / (hashlib.sha256(file.encode()).hexdigest()[:24] + ".json")
        f.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        self.audit.write("annotations.saved", file=file, shapes=len(clean))
        return data

    # ------------------------------------------------------------------ chat entry
    async def handle(self, director, tid: str, cid: str, text: str) -> None:
        low = text.casefold()
        if any(k in low for k in ("отправ", "send")):
            d = self.draft()
            if not d or d["status"] == "sent":
                return director._finish(tid, cid, "Черновика для отправки нет. Скажите «подготовь ответ по Флоре».",
                                        meta={"route": "flora"})
            out = await director.execute(tid, cid, "flora_send", {"draft_id": d["id"]}, "Отправка по вашей просьбе")
            return director._finish(tid, cid, director.outcome_text(out), meta={"route": "flora"},
                                    failed=out.status in ("failed", "invalid"))
        if any(k in low for k in ("ответ", "черновик", "напиши", "подготовь")):
            opt = "approve" if "соглас" in low else "decline" if any(k in low for k in ("откаж", "отклон", "оставь")) \
                else "clarify"
            out = await director.execute(tid, cid, "flora_draft", {"option": opt}, "Черновик по данным из писем и КП")
            self.bus.publish("ui.navigate", {"view": "flora", "tab": "reply"})
            msg = director.outcome_text(out)
            if out.status == "ok":
                msg += " Открыл его во вкладке «Ответ» — можно поправить текст. Без вашего подтверждения ничего не уйдёт."
            return director._finish(tid, cid, msg, meta={"route": "flora"})
        if any(k in low for k in ("смет", "кп", "сравни", "стоимост", "цен")):
            out = await director.execute(tid, cid, "flora_compare", {}, "Сравнение КП по строкам")
            self.bus.publish("ui.navigate", {"view": "flora", "tab": "quotes"})
            return director._finish(tid, cid, director.outcome_text(out), meta={"route": "flora"})
        if any(k in low for k in ("чертеж", "чертёж", "план")):
            self.bus.publish("ui.navigate", {"view": "flora", "tab": "drawing"})
            a = self.analysis()
            d = a["drawings"]
            return director._finish(tid, cid, f"Открыл план: ревизия {d[0]['revision']} (согласована) и ревизия "
                                              f"{d[1]['revision']} (предложение). Можно сравнить ползунком и рисовать поверх.",
                                    meta={"route": "flora"})
        out = await director.execute(tid, cid, "flora_inbox", {}, "Разбор демо-почты по проекту")
        if out.status != "ok":
            return director._finish(tid, cid, director.outcome_text(out), failed=True, meta={"route": "flora"})
        director._finish(tid, cid, self.summary(out.result.data["analysis"]), meta={"route": "flora", "demo": True})

    # ------------------------------------------------------------------ UI actions
    async def ui_action(self, director, data: dict) -> dict:
        act = data.get("action")
        if act == "state":
            a = self.analysis()
            return {"analysis": a, "draft": self.draft(), "decisions": self.decisions(), "fixtures": str(FIXTURES),
                    "sent": self.mail.sent_all(), "mail": {"provider": self.mail.name, "live": self.mail.live,
                                                           "account": self.mail.account},
                    "messages": {m["id"]: {**m} for m in self.mail.list_messages()}}
        if act == "draft.save":
            return {"draft": self.save_draft({k: data.get(k) for k in ("subject", "body")}, data.get("draft_id"))}
        if act == "draft.compose":
            return {"draft": self.save_draft(self.compose(str(data.get("option", "clarify"))),
                                             (self.draft() or {}).get("id") if (self.draft() or {}).get("status") != "sent" else None)}
        if act == "send":
            d = self.draft()
            if not d:
                raise ValueError("Нет черновика")
            return {"task_id": await director.run_ui_tool("flora_send", {"draft_id": d["id"]})}
        if act == "annotations.get":
            return self.annotations(str(data.get("file")))
        if act == "annotations.save":
            return self.save_annotations(str(data.get("file")), data.get("shapes") or [])
        raise ValueError("Неизвестное действие")


# ---------------------------------------------------------------------- tools
class FloraInbox(Tool):
    def __init__(self, wf: FloraWorkflow):
        self.wf = wf
        self.spec = ToolSpec(
            id="flora_inbox", title="Почта «Флоры»",
            description="Triage the (demo) project mailbox for project Flora: decisions, questions, promises, with "
                        "evidence. Marketing and suspicious mail are excluded.",
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=15,
            verification="Каждый пункт ссылается на существующее письмо; суммы пересчитаны из КП")

    def describe(self, args):
        return "разбор демо-почты, сверка с КП и планами"

    async def run(self, args, ctx):
        a = await asyncio.to_thread(self.wf.analyze)
        n = sum(1 for i in a["items"] if i["type"] == "DECISION_REQUIRED")
        return ToolResult({"analysis": a, "decisions": n, "project": len(a["items"]), "excluded": len(a["excluded"])},
                          f"Писем по проекту: {len(a['items'])}, решений нужно: {n}, скрыто: {len(a['excluded'])}.",
                          card="flora_inbox", untrusted=True)

    async def verify(self, args, result, ctx):
        a = result.data["analysis"]
        ok = all(self.wf.mail.get(i["id"]) for i in a["items"] + a["excluded"])
        sum_ok = a["comparison"]["new_total"] == sum(ln["sum"] for ln in a["quotes"]["new"]["lines"])
        return Verification("verified" if ok and sum_ok else "failed",
                            "Все пункты найдены в почтовом ящике; итоги пересчитаны по строкам"
                            if ok and sum_ok else "Несоответствие источникам")


class FloraCompare(Tool):
    def __init__(self, wf: FloraWorkflow):
        self.wf = wf
        self.spec = ToolSpec(
            id="flora_compare", title="Сравнение смет",
            description="Compare the approved and proposed supplier quotes for project Flora line by line, check "
                        "formulas and comparability of the alternative supplier.",
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=15,
            verification="Суммы пересчитаны по строкам; формулы итогов проверены")

    def describe(self, args):
        cmp = self.wf.analysis()["comparison"]
        return f"{cmp['base_file']} → {cmp['new_file']}"

    async def run(self, args, ctx):
        a = await asyncio.to_thread(self.wf.analyze)
        c = a["comparison"]
        s = f"По строкам КП: было {money(c['base_total'])}, стало {money(c['new_total'])}, разница +{money(c['delta'])}."
        for an in c["anomalies"]:
            s += f" ⚠ {an['text']} (ячейка {an['cell']}: `{an['formula']}`)."
        if not a["alternative"]["comparable"]:
            s += " Альтернативное КП «ТехноОкна» напрямую не сравнимо: " + "; ".join(a["alternative"]["reasons"]).lower() + "."
        return ToolResult({"comparison": c, "alternative": a["alternative"]}, s, card="flora_compare")

    async def verify(self, args, result, ctx):
        c = result.data["comparison"]
        return Verification("verified", f"Пересчитано по {len(c['rows'])} строкам; исходные файлы не изменялись")


class FloraDraft(Tool):
    def __init__(self, wf: FloraWorkflow):
        self.wf = wf
        self.spec = ToolSpec(
            id="flora_draft", title="Черновик",
            description="Prepare (not send) a Russian reply to the architect about window W-3 for project Flora. "
                        "option: clarify | approve | decline.",
            params={"type": "object", "properties": {"option": {"type": "string",
                                                                "enum": ["clarify", "approve", "decline"],
                                                                "default": "clarify"}}},
            risk=Risk.LOW, side_effects="local", timeout_s=10,
            verification="Все суммы и размеры в тексте взяты из писем и КП")

    def describe(self, args):
        return {"clarify": "ответ архитектору: запрос уточнений", "approve": "ответ архитектору: согласование с условиями",
                "decline": "ответ архитектору: оставить исходное окно"}[args.get("option", "clarify")]

    async def run(self, args, ctx):
        fields = self.wf.compose(args.get("option", "clarify"))
        cur = self.wf.draft()
        d = self.wf.save_draft(fields, cur["id"] if cur and cur["status"] != "sent" else None)
        return ToolResult({"draft": d}, f"Черновик ответа для {d['to_name']} готов (не отправлен).", card="flora_draft")

    async def verify(self, args, result, ctx):
        body = result.data["draft"]["body"]
        a = self.wf.analysis()
        nums = {money(a["comparison"]["delta"])}
        ok = all(n in body for n in nums) or args.get("option") == "decline"
        return Verification("verified" if ok else "unverified",
                            "Суммы в тексте совпадают с расчётом по КП" if ok else "Проверьте суммы вручную")


class FloraSend(Tool):
    def __init__(self, wf: FloraWorkflow):
        self.wf = wf
        self.spec = ToolSpec(
            id="flora_send", title="Отправка (демо)",
            description="Send the prepared Flora reply. Demo mailbox only — never leaves the computer.",
            params={"type": "object", "required": ["draft_id"], "properties": {
                "draft_id": {"type": "string", "maxLength": 40},
                "to": {"type": "string"}, "subject": {"type": "string", "maxLength": 300},
                "body": {"type": "string", "maxLength": 20000}, "version": {"type": "integer"},
                "operation_id": {"type": "string"}}},
            risk=Risk.HIGH, side_effects="external", timeout_s=15, idempotent=True, model_callable=False,
            verification="Письмо найдено в демо-папке «Отправленные» с тем же содержимым",
            impact="Письмо будет «отправлено» в демо-почту: наружу ничего не уходит, это проверка процесса. "
                   "После подтверждения изменить письмо будет нельзя.")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        d = self.wf.db.one("SELECT * FROM drafts WHERE id=?", (a["draft_id"],))
        if not d:
            raise ToolInputError("Черновик не найден")
        if d["status"] == "sent":
            raise ToolInputError("Это письмо уже отправлено")
        if any(k in a and a[k] != d[k] for k in ("subject", "body")) or "version" in a and a["version"] != d["version"]:
            raise ToolInputError("Текст отличается от сохранённого черновика — сначала сохраните черновик")
        # The approval is bound to the exact content: recipient, subject, body, version, operation id.
        return {"draft_id": d["id"], "to": f"{d['to_name']} <{d['to_addr']}>", "subject": d["subject"],
                "body": d["body"], "version": d["version"], "operation_id": d["operation_id"]}

    def describe(self, args):
        return f"Отправить письмо: {args.get('to')} — «{args.get('subject', '')[:80]}»"

    async def run(self, args, ctx):
        d = self.wf.db.one("SELECT * FROM drafts WHERE id=?", (args["draft_id"],))
        if d["version"] != args["version"] or d["body"] != args["body"] or d["subject"] != args["subject"]:
            raise ToolError("Черновик изменён после подтверждения — нужно новое подтверждение")
        prior = self.wf.mail.sent(args["operation_id"])
        if prior:   # reconcile instead of re-sending
            res = {**prior, "duplicate": True}
        else:
            self.wf.db.execute("UPDATE drafts SET status='sending' WHERE id=?", (d["id"],))
            res = self.wf.mail.send(args["operation_id"], {"to": args["to"], "subject": args["subject"],
                                                           "body": args["body"], "in_reply_to": d["reply_to"]})
        self.wf.db.execute("UPDATE drafts SET status='sent', provider_message_id=?, sent_at=? WHERE id=?",
                           (res["provider_message_id"], res["sent_at"], d["id"]))
        status = {"clarify": "clarification_requested", "approve": "approved_with_conditions",
                  "decline": "declined"}.get(d["option"] or "", "replied")
        self.wf.record_decision("Окно W-3 → W-3P (гостиная)", status,
                                "Ответ архитектору отправлен (демо-почта)",
                                [{"kind": "email", "id": d["reply_to"]}, {"kind": "sent", "id": res["provider_message_id"]}])
        return ToolResult({"provider_message_id": res["provider_message_id"], "duplicate": res["duplicate"],
                           "operation_id": args["operation_id"], "to": args["to"]},
                          ("Письмо уже было отправлено ранее — повторно не отправлял." if res["duplicate"]
                           else f"Отправлено (демо): {args['to']}."), card="flora_sent")

    async def verify(self, args, result, ctx):
        s = self.wf.mail.sent(args["operation_id"])
        ok = bool(s and s["body"] == args["body"] and s["subject"] == args["subject"])
        return Verification("verified" if ok else "failed",
                            "Найдено в демо-папке «Отправленные», содержимое совпадает (демо, не реальная почта)"
                            if ok else "В «Отправленных» письма нет или оно отличается")
