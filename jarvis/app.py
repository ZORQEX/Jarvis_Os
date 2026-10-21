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
    comms: object = None          # CommsHub


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
    from .agency.agency import Agency
    from .agency.tools import AgentGenerate, ClaudeChange, ClaudeInspect, OpenUrl
    from .providers.claude_code import ClaudeCodeProvider
    from .providers.registry import ProviderManager
    manager = ProviderManager(db, config, [ClaudeCodeProvider(config, db), gemini])
    for t in (AgentGenerate(manager), ClaudeInspect(manager, config), ClaudeChange(manager, config), OpenUrl()):
        registry.tools[t.spec.id] = t
    agency = Agency(manager, registry, config, db, bus, audit)
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
                        gateway=gateway, gemini=gemini, scheduler=scheduler, config=config, flora=flora,
                        providers=manager, agency=agency)
    from .voice.speech import SpeechManager
    from .voice.tts import DEFAULT_VOICE, PiperTTS, SileroTTS, VoiceEngine
    # Local voice only (Silero; Piper «Денис» as emergency fallback). The user's choice from Settings → Голос.
    chosen = db.get_kv("tts_voice") or DEFAULT_VOICE
    if not str(chosen).startswith("silero:"):
        chosen = DEFAULT_VOICE                      # older choices (Gemini / Piper voices) no longer exist
    engine = VoiceEngine(SileroTTS(), PiperTTS(config.piper_voice), chosen, float(db.get_kv("tts_rate") or 1.0))
    director.speech = SpeechManager(db, bus, engine)
    comms = build_comms(paths, config, db, bus, audit, registry, policy, gemini, director)
    recovered = tasks.recover_after_restart()
    approvals.expire_old()
    audit.write("core.start", recovered_tasks=len(recovered))
    return Core(paths, config, db, bus, audit, tasks, policy, approvals, registry, gateway, gemini, scheduler,
                director, recovered, comms)


def build_comms(paths, config, db, bus, audit, registry, policy, gemini, director):
    """Communication agent: Telegram, WhatsApp, e-mail behind one provider contract and one outbox."""
    import hashlib
    import os

    from . import secrets
    from .comms.agent import CommsAgent
    from .comms.email import EmailProvider
    from .comms.hub import CommsHub
    from .comms.telegram import TelegramProvider
    from .comms.tools import ContactSave, MessageCancel, MessageDeliver, MessageSend, TrustRuleAdd
    from .comms.whatsapp import WhatsAppProvider
    home = os.environ.get("JARVIS_HOME")
    store = secrets.Namespaced("dev-" + hashlib.sha256(home.encode()).hexdigest()[:8] + ":" if home else "")
    providers = [TelegramProvider(store), WhatsAppProvider(db, paths.data / "whatsapp"), EmailProvider(db, store)]
    hub = CommsHub(db, bus, audit, providers)
    for t in (MessageSend(hub), MessageDeliver(hub), MessageCancel(hub), TrustRuleAdd(hub), ContactSave(hub)):
        registry.tools[t.spec.id] = t
    policy.hooks.append(hub.policy_hook)
    hub.full_access = lambda: policy.full_access
    director.comms = CommsAgent(hub, gemini, config)
    director.hub = hub
    hub.outbox.deliver = director.deliver_job
    hub.outbox.on_missed = director.on_missed_message
    return hub
