"""Provider manager + model registry: the single place that knows which
providers and models exist, how they are configured, and how they performed."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from ..db import Database, now
from .base import CAP_RU, SENSITIVITY, AIProvider, Cap, Health, sens_rank


@dataclass
class ModelEntry:
    provider: str
    model: str
    role: str                    # main | fast | deep | tts | code
    capabilities: list[str]
    modalities: list[str]
    tool_support: bool
    context_limit: int | None = None
    enabled: bool = True
    priority: int = 5
    cost_in_per_mtok: float | None = None    # USD; None = unknown (not invented)
    cost_out_per_mtok: float | None = None
    verified: bool | None = None             # seen in the provider's model list


def model_registry(config) -> list[ModelEntry]:
    return [
        ModelEntry("claude", config.claude_chat_model, "main", ["general", "web_research", "vision", "desktop_tools",
                                                                "document_analysis", "writing", "translation", "voice"],
                   ["text", "image"], True),
        ModelEntry("claude", config.claude_fast_model, "fast", ["planning"], ["text"], False),
        ModelEntry("claude_code", config.claude_model or "default", "code", ["code", "terminal", "filesystem"],
                   ["text"], True),
    ]


DEFAULT_SETTINGS = {"enabled": True, "fallback_eligible": True, "max_sensitivity": "CONFIDENTIAL"}


class ProviderManager:
    def __init__(self, db: Database, config, providers: list[AIProvider]):
        self.db, self.config = db, config
        self.providers: dict[str, AIProvider] = {p.id: p for p in providers}
        self.models = model_registry(config)
        # Forget settings of providers that no longer exist (e.g. a removed integration).
        allset = self.db.get_kv("provider_settings", {}) or {}
        if set(allset) - set(self.providers):
            self.db.set_kv("provider_settings", {k: v for k, v in allset.items() if k in self.providers})

    # -- settings (user-controlled; no model or provider can change these) -----
    def settings(self, pid: str) -> dict:
        allset = self.db.get_kv("provider_settings", {}) or {}
        return {**DEFAULT_SETTINGS, **allset.get(pid, {})}

    def update_settings(self, pid: str, **kw) -> dict:
        if pid not in self.providers:
            raise ValueError("Неизвестный провайдер")
        allset = self.db.get_kv("provider_settings", {}) or {}
        cur = {**DEFAULT_SETTINGS, **allset.get(pid, {})}
        for k, v in kw.items():
            if k in ("enabled", "fallback_eligible") and isinstance(v, bool):
                cur[k] = v
            elif k == "max_sensitivity" and v in SENSITIVITY[:3]:   # HIGHLY_SENSITIVE never goes to a cloud
                cur[k] = v
        allset[pid] = cur
        self.db.set_kv("provider_settings", allset)
        return cur

    def health(self, pid: str) -> Health:
        p = self.providers[pid]
        if not self.settings(pid)["enabled"]:
            return Health.DISABLED
        return p.health()

    def get(self, pid: str) -> AIProvider | None:
        return self.providers.get(pid)

    # -- eligibility (hard constraints) -----------------------------------------
    def eligible(self, pid: str, cap: Cap, sensitivity: str, privacy_profile: str) -> tuple[bool, str]:
        p = self.providers.get(pid)
        if not p:
            return False, "провайдер не установлен"
        if cap not in p.capabilities:
            return False, f"{p.display_name} не умеет: {CAP_RU.get(cap, cap)}"
        h = self.health(pid)
        if h == Health.DISABLED:
            return False, f"{p.display_name} выключен в настройках"
        if h == Health.NOT_CONFIGURED:
            return False, f"{p.display_name} не подключён"
        if h not in (Health.CONNECTED, Health.DEGRADED):
            return False, f"{p.display_name} недоступен ({h.value})"
        if p.cloud and privacy_profile == "STRICT_OFFLINE":
            return False, "режим «Строго офлайн» запрещает облачных провайдеров"
        if sens_rank(sensitivity) > sens_rank(self.settings(pid)["max_sensitivity"]):
            return False, f"данные уровня {sensitivity} нельзя передавать в {p.display_name} (настройка приватности)"
        return True, "ok"

    # -- usage ledger ------------------------------------------------------------
    def record(self, pid: str, model: str | None, task_id: str | None, step_id: str | None, cap: str | None,
               usage: dict | None, cost: float | None, latency_ms: float | None, ok: bool, error_kind: str | None = None):
        u = usage or {}
        tin = u.get("input_tokens") or u.get("prompt_token_count")
        tout = u.get("output_tokens") or u.get("candidates_token_count")
        if cost is None:
            m = next((x for x in self.models if x.provider == pid and x.model == model), None)
            if m and m.cost_in_per_mtok is not None and tin is not None and tout is not None:
                cost = tin / 1e6 * m.cost_in_per_mtok + tout / 1e6 * (m.cost_out_per_mtok or 0)
        self.db.execute("INSERT INTO provider_usage(ts,provider,model,task_id,step_id,capability,input_tokens,"
                        "output_tokens,cost_usd,latency_ms,ok,error_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), pid, model, task_id, step_id, cap, tin, tout, cost, latency_ms, int(ok), error_kind))

    def failure_rate(self, pid: str, window_s: float = 3600) -> float:
        r = self.db.one("SELECT COUNT(*) n, SUM(1-ok) f FROM provider_usage WHERE provider=? AND ts>?",
                        (pid, time.time() - window_s))
        return (r["f"] or 0) / r["n"] if r and r["n"] else 0.0

    def latency_p50(self, pid: str) -> float | None:
        rows = [r["latency_ms"] for r in self.db.all(
            "SELECT latency_ms FROM provider_usage WHERE provider=? AND ok=1 AND latency_ms IS NOT NULL "
            "ORDER BY ts DESC LIMIT 50", (pid,))]
        rows.sort()
        return rows[len(rows) // 2] if rows else None

    def usage_summary(self, pid: str) -> dict:
        r = self.db.one("SELECT COUNT(*) calls, SUM(ok) ok, SUM(input_tokens) tin, SUM(output_tokens) tout, "
                        "SUM(cost_usd) cost, MAX(ts) last FROM provider_usage WHERE provider=?", (pid,))
        last_err = self.db.one("SELECT error_kind, ts FROM provider_usage WHERE provider=? AND ok=0 "
                               "ORDER BY ts DESC LIMIT 1", (pid,))
        return {"calls": r["calls"] or 0, "ok": r["ok"] or 0, "input_tokens": r["tin"], "output_tokens": r["tout"],
                "cost_usd": round(r["cost"], 4) if r["cost"] is not None else None, "last_used": r["last"],
                "last_error": dict(last_err) if last_err else None, "latency_p50_ms": self.latency_p50(pid)}

    def describe(self) -> list[dict]:
        out = []
        for pid, p in self.providers.items():
            d = p.describe()
            d.update(id=pid, name=p.display_name, role=p.role_ru, health=self.health(pid).value,
                     capabilities=[{"id": c.value, "ru": CAP_RU.get(c, c.value)} for c in sorted(p.capabilities)],
                     settings=self.settings(pid), usage=self.usage_summary(pid), cloud=p.cloud,
                     models=[asdict(m) for m in self.models if m.provider == pid])
            out.append(d)
        return out
