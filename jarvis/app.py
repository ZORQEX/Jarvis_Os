"""Composition root: builds the core services once."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Paths
from .db import Database
from .director import CloudConsent, Director
from .events import AuditLog, EventBus
from .gateway import Gateway, Registry
from .policy import Approvals, PolicyEngine
from .providers.claude_chat import ClaudeChatProvider
from .scheduler import ReminderCreate, ScheduledChange, Scheduler, TaskSchedule
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
    claude: ClaudeChatProvider    # the conversational AI (Claude via the local Claude Code CLI)
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
             WorkspaceSwitch(), VolumeSet(), Screenshot(), Notify(), ReminderCreate(scheduler), CloudConsent(),
             TaskSchedule(scheduler), ScheduledChange(scheduler)]
    from .tools.fileops import FileTrash, FileWrite, FolderCreate, FolderList, ShellRead
    from .tools.install import SystemInstall
    tools.append(SystemInstall())               # system programs: the user types the password in the system dialog
    tools += [FolderCreate(), FileWrite(config), FolderList(), FileTrash(), ShellRead()]   # quick file actions
    from .tools.window import WindowShow
    tools.append(WindowShow(bus))               # show a workspace of the JARVIS window (the server sets presence)
    tools += extra_tools or []
    registry = Registry(tools)
    gateway = Gateway(registry, policy, approvals, tasks, bus, audit, config)
    claude = ClaudeChatProvider(config, db, paths.state, registry)
    from .agency.agency import Agency
    from .agency.tools import AgentGenerate, ClaudeChange, ClaudeInspect, ComputerTask, OpenUrl
    from .providers.claude_code import ClaudeCodeProvider
    from .providers.registry import ProviderManager
    manager = ProviderManager(db, config, [ClaudeCodeProvider(config, db), claude])
    from .publish import GitHub, SitePublish
    github = GitHub()                            # site publishing through the user's own GitHub account
    for t in (AgentGenerate(manager), ClaudeInspect(manager, config), ClaudeChange(manager, config),
              ComputerTask(manager, config), OpenUrl(), SitePublish(github, config)):
        registry.tools[t.spec.id] = t
    from .skills import SkillCreate, SkillList, SkillRun     # abilities JARVIS builds for itself
    for t in (SkillList(config), SkillRun(config), SkillCreate(manager, config)):
        registry.tools[t.spec.id] = t
    from .improve import ImproveIssues, SelfImprove, SelfImproveApply     # JARVIS improves its own code
    for t in (ImproveIssues(db), SelfImprove(manager, config), SelfImproveApply(config)):
        registry.tools[t.spec.id] = t
    agency = Agency(manager, registry, config, db, bus, audit)
    director = Director(db=db, bus=bus, audit=audit, tasks=tasks, policy=policy, approvals=approvals,
                        gateway=gateway, ai=claude, scheduler=scheduler, config=config,
                        providers=manager, agency=agency)
    from .voice.speech import SpeechManager
    from .voice.tts import DEFAULT_VOICE, VOICES, KokoroTTS, PiperTTS, SileroTTS, SupertonicTTS, VoiceEngine
    # Local voices only: Supertonic (one voice, every language), Silero / Kokoro / Piper as fallback and as choices.
    # The user's choice from Settings → Голос.
    supertonic = SupertonicTTS()
    chosen = db.get_kv("tts_voice") or DEFAULT_VOICE
    if not supertonic.available() and str(chosen).startswith("supertonic:"):
        chosen = "silero:aidar" if chosen[-2] == "M" else "silero:xenia"      # not installed: the Russian voice
    elif supertonic.available() and not db.get_kv("voice_supertonic_switched"):
        # The person asked for one natural voice for every language (2026-10-24): switch once to the measured best
        # (male, like before: M4; female: F1); a later choice in Settings stays.
        g = next((v["gender"] for v in VOICES if v["id"] == chosen), "м")
        chosen = "supertonic:M4" if g == "м" else "supertonic:F1"
        db.set_kv("tts_voice", chosen)
        db.set_kv("voice_supertonic_switched", True)
    if not any(v["id"] == chosen for v in VOICES):
        chosen = DEFAULT_VOICE if supertonic.available() else "silero:aidar"   # older (cloud / Piper) choices
    engine = VoiceEngine(SileroTTS(), PiperTTS(config.piper_voice), chosen, float(db.get_kv("tts_rate") or 1.0),
                         kokoro=KokoroTTS(), en_voice_id=db.get_kv("tts_voice_en"), supertonic=supertonic)
    director.speech = SpeechManager(db, bus, engine)
    from .voice.stt import LocalSTT
    director.stt = LocalSTT()           # speech recognition on this computer (GigaAM + Parakeet) — the only one
    comms = build_comms(paths, config, db, bus, audit, registry, policy, claude, director)
    from .history import ActionHistory
    director.history = ActionHistory(db, bus)       # lasting actions only (sites, code, files, programs, notes)
    director.github = github
    director.history.backfill(tasks)
    recovered = tasks.recover_after_restart()
    approvals.expire_old()
    audit.write("core.start", recovered_tasks=len(recovered))
    return Core(paths, config, db, bus, audit, tasks, policy, approvals, registry, gateway, claude, scheduler,
                director, recovered, comms)


def build_comms(paths, config, db, bus, audit, registry, policy, ai, director):
    """Communication agent: Telegram, WhatsApp, e-mail behind one provider contract and one outbox."""
    import hashlib
    import os

    from . import secrets
    from .comms.agent import CommsAgent
    from .comms.email import EmailProvider
    from .comms.hub import CommsHub
    from .comms.telegram import TelegramProvider
    from .comms.tools import ContactFind, ContactSave, MessageCancel, MessageDeliver, MessageSend, TrustRuleAdd
    from .comms.whatsapp import WhatsAppProvider
    home = os.environ.get("JARVIS_HOME")
    store = secrets.Namespaced("dev-" + hashlib.sha256(home.encode()).hexdigest()[:8] + ":" if home else "")
    providers = [TelegramProvider(store), WhatsAppProvider(db, paths.data / "whatsapp"), EmailProvider(db, store)]
    hub = CommsHub(db, bus, audit, providers)
    for t in (MessageSend(hub), MessageDeliver(hub), MessageCancel(hub), TrustRuleAdd(hub), ContactSave(hub),
              ContactFind(hub)):
        registry.tools[t.spec.id] = t
    policy.hooks.append(hub.policy_hook)
    hub.full_access = lambda: policy.full_access
    director.comms = CommsAgent(hub, ai, config)
    director.hub = hub
    wt = registry.get("window_show")
    if wt:          # a shown messenger workspace tells the model exactly what it needs (no invented steps)
        views = {"telegram": "telegram", "whatsapp": "whatsapp", "mail": "email"}

        def hint(view: str) -> str:
            ch = views.get(view)
            if not ch:
                return ""
            p = hub.provider(ch)
            st = p.status()
            if st.state == "connected":
                return f"{p.title} подключён к JARVIS."
            where = {"telegram": " Страница для api_id и api_hash: https://my.telegram.org/apps (открой её через open_url).",
                     "email": " Пароль приложения Gmail: https://myaccount.google.com/apppasswords (открой через open_url).",
                     }.get(ch, "")
            return f"{p.title} не подключён ({st.detail or st.state}). Что нужно в этом разделе: {st.requirement}{where}"

        async def start(view: str) -> str:
            if views.get(view) != "whatsapp":
                return ""
            p = hub.provider("whatsapp")
            if p.connected() or not hasattr(p, "begin_link"):
                return ""
            await p.begin_link()
            hub.publish_status()
            return "QR-код WhatsApp появляется в этом разделе — его нужно отсканировать телефоном."
        wt.hint, wt.starter = hint, start
    hub.outbox.deliver = director.deliver_job
    hub.outbox.on_missed = director.on_missed_message
    return hub
