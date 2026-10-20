"""Director decision object: the structured, validated plan for one task."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..providers.base import SENSITIVITY, Cap

ACTIONS = {
    # action: (capability, executor kind)
    "research": (Cap.WEB_RESEARCH, "provider"),
    "analyze": (Cap.DOC_ANALYSIS, "provider"),
    "write": (Cap.WRITING, "provider"),
    "translate": (Cap.TRANSLATION, "provider"),
    "vision_screen": (Cap.VISION, "provider"),
    "chat": (Cap.GENERAL, "provider"),           # conversation + JARVIS desktop tools via function calling
    "inspect_code": (Cap.CODE, "provider"),
    "change_code": (Cap.CODE, "provider"),
    "compare_estimates": (None, "tool"),         # deterministic: flora_compare
    "read_mail": (None, "tool"),                 # deterministic: mail connector
    "send_message": (None, "tool"),              # external side effect: approval-gated
}
RISKS = ["low", "moderate", "high"]
CLASSES = {"research", "business_communication", "document_analysis", "software", "visual_diagnosis",
           "conversation", "mail", "writing", "mixed"}


@dataclass
class Step:
    id: str
    action: str
    instruction: str
    capability: str | None = None
    executor_kind: str = "provider"
    executor: str | None = None        # provider id or tool id, filled by the router
    reason: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "planned"
    candidates: list[dict] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    result_ref: str | None = None      # task_steps.id holding the output
    verification: dict | None = None
    approval: str = "not_required"     # not_required | required | approved | denied


@dataclass
class Decision:
    task_id: str
    objective: str
    classification: str
    risk: str
    sensitivity: str
    mode: str                          # tools | single_ai | multi_ai | ai_tools
    steps: list[Step]
    requires_approval: bool = False
    verification: list[str] = field(default_factory=list)
    planner: str = "heuristic"
    override: str | None = None
    notes: list[str] = field(default_factory=list)
    step_sensitivity: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class DecisionError(ValueError):
    pass


def validate(d: Decision, known_providers: set[str], known_tools: set[str]) -> Decision:
    if not d.steps or len(d.steps) > 8:
        raise DecisionError("План должен содержать от 1 до 8 шагов")
    if d.risk not in RISKS or d.sensitivity not in SENSITIVITY:
        raise DecisionError("Неверный риск или уровень данных")
    ids = set()
    for s in d.steps:
        if s.action not in ACTIONS:
            raise DecisionError(f"Неизвестное действие: {s.action}")
        cap, kind = ACTIONS[s.action]
        s.capability = cap.value if cap else None
        s.executor_kind = kind
        if s.id in ids:
            raise DecisionError("Повторяющийся id шага")
        for dep in s.depends_on:
            if dep not in ids:
                raise DecisionError(f"Шаг {s.id} зависит от неизвестного или более позднего шага {dep}")
        if s.executor and kind == "provider" and s.executor not in known_providers:
            raise DecisionError(f"Неизвестный исполнитель {s.executor}")
        if s.executor and kind == "tool" and s.executor not in known_tools:
            raise DecisionError(f"Неизвестный инструмент {s.executor}")
        if not s.instruction or len(s.instruction) > 2000:
            raise DecisionError("Пустое или слишком длинное задание шага")
        ids.add(s.id)
    # Policy-derived fields are recomputed, never taken from a model.
    if any(s.action in ("send_message", "change_code") for s in d.steps):
        d.requires_approval = True
        d.risk = "high"
    providers = {s.capability for s in d.steps if s.executor_kind == "provider"}
    d.mode = ("tools" if not providers else "ai_tools" if any(s.executor_kind == "tool" for s in d.steps)
              else "multi_ai" if len(d.steps) > 1 else "single_ai")
    return d
