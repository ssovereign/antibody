"""Service container wiring every subsystem. Used by the API, CLI, tests and
evaluation sandboxes (each sandbox gets its own Core with its own storage)."""
from __future__ import annotations

from .chat import ChatService
from .claude import ClaudeConnection
from .commands import Commands
from .config import Config
from .context.compiler import ContextCompiler
from .effects import EffectService
from .evaluation.evaluator import Evaluator
from .executors.registry import Executors
from .gateway.auditor import build_auditor
from .gateway.gateway import Gateway
from .health.monitor import HealthMonitor, Incidents
from .ingress.pipeline import SourceService
from .jobs import JobQueue
from .journal import Journal
from .learning.service import LearningService
from .learning.attribution import Attribution
from .projects import ProjectService
from .registry.registry import Registry
from .runtime.agent import ReferenceAgent
from .runtime.coordinator import Coordinator
from .storage.db import Database
from .storage.objects import ObjectStore
from .versions.merge import MergeService
from .versions.store import VersionStore


class Core:
    def __init__(self, config: Config | None = None, start_workers: bool = False, register_suites: bool = True, recover: bool = True):
        self.config = config or Config.from_env()
        self.db = Database(self.config.DATABASE_PATH)
        self.objects = ObjectStore(self.config.OBJECT_STORE_ROOT)
        self.journal = Journal(self.db, self.objects)
        self.projects = ProjectService(self.db, self.journal)
        self.sources = SourceService(self.db, self.objects, self.config)
        self.registry = Registry(self)
        self.claude = ClaudeConnection(self.config)
        self.auditor = build_auditor(self.config.AUDITOR_PROVIDER, self.config.MODEL_ID, self.config.GUARD_TIMEOUT_SECONDS)
        self.executors = Executors(self)
        self.incidents = Incidents(self)
        self.health = HealthMonitor(self)
        self.compiler = ContextCompiler(self)
        self.versions = VersionStore(self)
        self.merges = MergeService(self)
        self.effects = EffectService(self)
        self.learning = LearningService(self)
        self.evaluator = Evaluator(self)
        self.attribution = Attribution(self)
        self.jobs = JobQueue(self)
        self.coordinator = Coordinator(self)
        self.gateway = Gateway(self)
        self.agent = ReferenceAgent(self)
        self.chat = ChatService(self)
        self.commands = Commands(self)
        self.refresh_auditor()
        self.worker_identity = {"role": "worker", "actor_id": "worker:local", "project_id": None}
        self.propagate_runtime_errors = False
        self.gateway.on_decision.append(self.health.on_decision)
        self.gateway.on_effect.append(self.coordinator.on_effect)
        self.gateway.on_effect.append(self.health.on_effect)
        if not self.config.AUTO_RECOVERY:
            self.coordinator.request_recovery = self._pause_instead_of_recovery
        self.jobs.register("run", lambda p: {"status": self.coordinator.run_loop(p["run_id"])["status"]})
        self.jobs.register("evaluate", lambda p: {"evaluation_id": self.learning.evaluate(p["project_id"], p["candidate_id"], p.get("suite"), p.get("actor", "app"))["evaluation_id"]})
        if register_suites:
            self.evaluator.register_suites()
        # a secondary process (e.g. the MCP server) must not pause runs owned by the main service
        self.recovery_report = self.coordinator.startup_recovery() if recover else {"skipped": True}
        if start_workers and self.config.WORKER_THREADS > 0:
            self.jobs.start(self.config.WORKER_THREADS)

    def refresh_auditor(self) -> str:
        """Use Claude as the constrained semantic auditor when the operator chose it and Claude is connected."""
        from .gateway.auditor import AuditRunner, ClaudeAuditor, RulesAuditor
        if self.claude.settings().get("auditor") == "claude" and self.claude.connected():
            impl = ClaudeAuditor(self.claude)
        else:
            impl = RulesAuditor()
        self.auditor = AuditRunner(impl, self.config.GUARD_TIMEOUT_SECONDS)
        if hasattr(self, "gateway"):
            self.gateway.auditor = self.auditor
        return impl.name

    def integration_state(self, name: str, project_id: str | None = None) -> dict:
        if name == "google_calendar":
            from .ingress.calendar_live import GoogleCalendarAdapter
            return GoogleCalendarAdapter(self, project_id or "").state()
        return {"state": "unknown"}

    def _pause_instead_of_recovery(self, run_id, incident, candidate):
        self.health.transition(run_id, "Paused", "REPEAT_FAILURE_LIMIT", {"incident_id": incident["incident_id"], "note": "automatic recovery disabled"})
        self.coordinator.set_status(run_id, "paused", "REPEAT_FAILURE_LIMIT")

    def shutdown(self) -> None:
        """Safe shutdown barrier: stop workers, then pause active runs so nothing dispatches unguarded."""
        self.jobs.stop()
        for r in self.db.all("SELECT run_id FROM runs WHERE status IN ('running','recovering')"):
            self.coordinator.set_status(r["run_id"], "paused", "SERVICE_SHUTDOWN")

    def close(self) -> None:
        self.jobs.stop()
        self.agent.close()
        self.db.close()
