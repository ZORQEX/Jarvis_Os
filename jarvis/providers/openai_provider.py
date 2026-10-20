"""OpenAI (GPT) adapter — official `openai` SDK, Responses API.

Role in JARVIS: writing, documents, analysis, translation, messages. Requests
are sent with store=False. Function calls are executed by JARVIS's gateway,
never by the SDK."""
from __future__ import annotations

import asyncio
import base64
import json
import time

from .. import secrets
from .base import AIProvider, Cap, GenResult, Health, ProviderFailure, StepRequest
from .gemini import AGENCY_SYSTEM, context_block


def classify_openai(e: Exception) -> ProviderFailure:
    if isinstance(e, ProviderFailure):
        return e
    import openai
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderFailure("auth", "Ключ OpenAI отклонён или нет доступа")
    if isinstance(e, openai.RateLimitError):
        return ProviderFailure("quota", "OpenAI: превышен лимит запросов или исчерпана квота")
    if isinstance(e, openai.NotFoundError):
        return ProviderFailure("model", "Модель OpenAI недоступна для этого ключа")
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, asyncio.TimeoutError)):
        return ProviderFailure("network", "Нет связи с OpenAI")
    if isinstance(e, openai.InternalServerError):
        return ProviderFailure("server", "Сбой на стороне OpenAI")
    if isinstance(e, openai.BadRequestError):
        return ProviderFailure("invalid", "OpenAI отклонил запрос: " + str(e)[:200])
    return ProviderFailure("invalid", "Ошибка OpenAI: " + str(e)[:200])


class OpenAIProvider(AIProvider):
    id = "openai"
    display_name = "GPT"
    role_ru = "Письма · документы · анализ · перевод · сообщения"
    capabilities = {Cap.WRITING, Cap.TRANSLATION, Cap.DOC_ANALYSIS, Cap.GENERAL, Cap.DESKTOP_TOOLS,
                    Cap.WEB_RESEARCH, Cap.VISION, Cap.PLANNING}
    modalities = {"text", "image"}
    cloud = True

    def __init__(self, config):
        self.config = config
        self._client = None
        self._fp = None
        self.status = "unknown"
        self.last_error: str | None = None
        self.last_error_kind: str | None = None
        self.checked_at: float | None = None
        self.available: dict[str, bool | None] = {}

    def configured(self) -> bool:
        return secrets.get("openai") is not None

    def client(self):
        key = secrets.get("openai")
        if not key:
            raise ProviderFailure("unavailable", "OpenAI не подключён")
        fp = str(hash(key))
        if self._client is None or fp != self._fp:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=key, timeout=self.config.ai_timeout_s, max_retries=1)
            self._fp = fp
        return self._client

    def reset(self):
        self._client, self._fp, self.status = None, None, "unknown"
        self.last_error = self.last_error_kind = None
        self.available = {}

    def health(self) -> Health:
        if not self.configured():
            return Health.NOT_CONFIGURED
        if self.status == "error":
            return {"auth": Health.AUTH_ERROR, "quota": Health.RATE_LIMITED, "network": Health.OFFLINE}.get(
                self.last_error_kind or "", Health.DEGRADED)
        return Health.CONNECTED if self.status == "ready" else Health.DEGRADED

    async def health_check(self, live: bool = False) -> dict:
        try:
            ids = set()
            async for m in self.client().models.list():
                ids.add(m.id)
            c = self.config
            self.available = {m: m in ids for m in (c.openai_model_main, c.openai_model_fast, c.openai_model_deep)}
            ok = self.available[c.openai_model_main] or self.available[c.openai_model_fast]
            self.status = "ready" if ok else "error"
            self.last_error = None if ok else "Настроенные модели GPT недоступны для этого ключа"
            self.last_error_kind = None if ok else "model"
        except ProviderFailure as e:
            self.last_error, self.last_error_kind = str(e), e.kind
        except Exception as e:   # noqa: BLE001
            pe = classify_openai(e)
            self.status, self.last_error, self.last_error_kind = "error", str(pe), pe.kind
        self.checked_at = time.time()
        return self.describe()

    def model_for(self, cap: Cap) -> str:
        c = self.config
        if cap == Cap.PLANNING and self.available.get(c.openai_model_fast) is not False:
            return c.openai_model_fast
        if self.available.get(c.openai_model_main) is False and self.available.get(c.openai_model_fast):
            return c.openai_model_fast
        return c.openai_model_main

    def _content(self, req: StepRequest) -> list:
        text = (context_block(req.context) + "\n\n" if req.context else "") + req.instruction
        parts = [{"type": "input_text", "text": text}]
        if req.image:
            parts.append({"type": "input_image",
                          "image_url": f"data:{req.image_mime};base64,{base64.b64encode(req.image).decode()}"})
        return [{"role": "user", "content": parts}]

    async def generate(self, req: StepRequest, on_delta=None, on_progress=None) -> GenResult:
        model = self.model_for(req.capability)
        tools = [{"type": "web_search"}] if req.capability == Cap.WEB_RESEARCH else None
        t0 = time.perf_counter()
        first = None
        text: list[str] = []
        final = None
        try:
            stream = await self.client().responses.create(
                model=model, instructions=req.system or AGENCY_SYSTEM, input=self._content(req), stream=True,
                store=False, max_output_tokens=req.max_tokens, **({"tools": tools} if tools else {}))
            async for ev in stream:
                if ev.type == "response.output_text.delta":
                    if first is None:
                        first = (time.perf_counter() - t0) * 1000
                    text.append(ev.delta)
                    if on_delta:
                        on_delta(ev.delta)
                elif ev.type == "response.completed":
                    final = ev.response
                elif ev.type in ("response.failed", "error"):
                    raise ProviderFailure("server", "OpenAI вернул ошибку ответа")
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001
            pe = classify_openai(e)
            self.last_error, self.last_error_kind = str(pe), pe.kind
            if pe.kind in ("auth", "quota", "network"):
                self.status = "error"
            raise pe from None
        self.status = "ready"
        sources, usage = [], {}
        if final is not None:
            if final.usage:
                usage = {"input_tokens": final.usage.input_tokens, "output_tokens": final.usage.output_tokens}
            for item in final.output or []:
                for c in getattr(item, "content", None) or []:
                    for a in getattr(c, "annotations", None) or []:
                        if getattr(a, "type", "") == "url_citation" and not any(s["url"] == a.url for s in sources):
                            sources.append({"title": getattr(a, "title", "") or a.url, "url": a.url})
        return GenResult("".join(text).strip(), "openai", model, sources, usage, None, first,
                         (time.perf_counter() - t0) * 1000)

    async def run_tool_loop(self, system: str, history: list[dict], user_text: str, tools: list, execute,
                            on_delta=None, max_rounds: int = 6, should_stop=lambda: False,
                            checkpoint=None, on_thinking=None, role: str = "main") -> GenResult:
        model = self.config.openai_model_main
        fns = [{"type": "function", "name": t.spec.id, "description": t.spec.description,
                "parameters": t.spec.params} for t in tools]
        items: list = [{"role": "assistant" if m["role"] != "user" else "user", "content": m["text"]} for m in history]
        items.append({"role": "user", "content": user_text})
        full: list[str] = []
        usage_total = {"input_tokens": 0, "output_tokens": 0}
        first, rounds, t0 = None, 0, time.perf_counter()
        while True:
            rounds += 1
            if on_thinking:
                on_thinking(True)
            final = None
            try:
                stream = await self.client().responses.create(
                    model=model, instructions=system, input=items, tools=fns or None, stream=True, store=False,
                    include=["reasoning.encrypted_content"])
                async for ev in stream:
                    if ev.type == "response.output_text.delta":
                        if on_thinking:
                            on_thinking(False)
                        first = first or (time.perf_counter() - t0) * 1000
                        full.append(ev.delta)
                        if on_delta:
                            on_delta(ev.delta)
                    elif ev.type == "response.completed":
                        final = ev.response
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001
                pe = classify_openai(e)
                self.last_error, self.last_error_kind = str(pe), pe.kind
                if pe.kind in ("auth", "quota", "network"):
                    self.status = "error"
                raise pe from None
            if on_thinking:
                on_thinking(False)
            self.status = "ready"
            if final is None:
                break
            if final.usage:
                usage_total["input_tokens"] += final.usage.input_tokens
                usage_total["output_tokens"] += final.usage.output_tokens
            calls = [o for o in final.output or [] if getattr(o, "type", "") == "function_call"]
            items += [o.model_dump(exclude_none=True) for o in final.output or []]
            if not calls:
                break
            if rounds >= max_rounds:
                full.append("\n\n_Остановился: достигнут лимит шагов для одной задачи._")
                break
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except ValueError:
                    args = {}
                res = await execute(c.name, args)
                items.append({"type": "function_call_output", "call_id": c.call_id,
                              "output": json.dumps(res, ensure_ascii=False, default=str)})
                if should_stop():
                    break
            if should_stop():
                full.append("\n\nОстановлено: включена экстренная остановка.")
                break
        return GenResult("".join(full).strip(), "openai", model, [], usage_total, None, first,
                         (time.perf_counter() - t0) * 1000, {"rounds": rounds})

    def describe(self) -> dict:
        return {"configured": self.configured(), "key_source": secrets.source("openai"), "key_settable": True,
                "auth": "API-ключ OpenAI в GNOME Keyring", "status": self.status, "last_error": self.last_error,
                "checked_at": self.checked_at, "models_available": self.available}
