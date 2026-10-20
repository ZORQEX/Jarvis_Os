"""Composition root: builds the core services once."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Paths
from .db import Database
from .director import CloudConsent, Director
from .events import AuditLog, EventBus
from .gateway import Gateway, Registry
from .policy import Approvals, PolicyEngine
from .providers.gemini import GeminiProvider
from .scheduler import ReminderCreate, Scheduler
from .tasks import TaskStore
from .tools.desktop import AppOpen, Notify, Screenshot, VolumeSet, WindowList, WorkspaceSwitch
from .tools.files import CreateNote, OpenPath, ReadFile, SearchFiles
from .tools.system import SystemInfo


@dataclass
class Core:
    paths: Paths
    config: Config
    db: Database
    bus: EventBus
    audit: AuditLog
    tasks: TaskStore
    policy: PolicyEngine
    approvals: Approvals
    registry: Registry
    gateway: Gateway
    gemini: GeminiProvider
    scheduler: Scheduler
    director: Director
    recovered: list[str]


def build(paths: Paths | None = None, config: Config | None = None, extra_tools: list | None = None) -> Core:
    paths = paths or Paths.detect()
    paths.ensure()
    config = config or Config.load(paths)
    db = Database(paths.db)
    bus = EventBus(db)
    audit = AuditLog(paths.audit_log)
    tasks = TaskStore(db, bus)
    policy = PolicyEngine()
    approvals = Approvals(db, bus, audit, config.approval_ttl_s)
    scheduler = Scheduler(db, bus, audit)
    tools = [SystemInfo(), SearchFiles(), ReadFile(), OpenPath(), CreateNote(), AppOpen(config.apps), WindowList(),
             WorkspaceSwitch(), VolumeSet(), Screenshot(), Notify(), ReminderCreate(scheduler), CloudConsent()]
    tools += extra_tools or []
    registry = Registry(tools)
    gateway = Gateway(registry, policy, approvals, tasks, bus, audit, config)
    gemini = GeminiProvider(config)
    flora = None
    try:
        from .flora.workflow import FloraWorkflow
        flora = FloraWorkflow(paths, db, bus, audit, gemini, config)
        flora.approvals = approvals
        for t in flora.tools():
            registry.tools[t.spec.id] = t
    except ImportError:
        pass
    director = Director(db=db, bus=bus, audit=audit, tasks=tasks, policy=policy, approvals=approvals,
                        gateway=gateway, gemini=gemini, scheduler=scheduler, config=config, flora=flora)
    recovered = tasks.recover_after_restart()
    approvals.expire_old()
    audit.write("core.start", recovered_tasks=len(recovered))
    return Core(paths, config, db, bus, audit, tasks, policy, approvals, registry, gateway, gemini, scheduler,
                director, recovered)
