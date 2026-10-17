"""Typed contracts shared by the service, runtime and API.

Strict models (extra="forbid") reject extra authority fields: a client or
model cannot smuggle run identity, actor roles or policy bindings into a
proposal (Technical Design: Proposed action).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

Origin = Literal["USER_ASSIGNMENT", "OPERATOR_POLICY", "EXTERNAL_DATA", "AGENT_DERIVED", "VERIFIED_TOOL_METADATA"]
Outcome = Literal["ALLOW", "DENY", "PAUSE"]
Mode = Literal["live", "recorded", "simulated"]
HealthState = Literal["Healthy", "Degraded", "Paused", "Quarantined", "Recovering", "Unknown"]
EffectState = Literal["RESERVED", "DISPATCHING", "COMPLETED", "FAILED", "UNKNOWN", "DENIED"]

REASON_CODES = {
    "OK": "Within the task authorization.",
    "TOOL_NOT_ALLOWED": "This tool is not part of the task authorization.",
    "SCOPE_VIOLATION": "The arguments fall outside the authorized scope.",
    "PATH_OUTSIDE_WORKSPACE": "The path resolves outside the authorized file roots.",
    "DESTINATION_NOT_AUTHORIZED": "This recipient is outside the task authorization.",
    "DATA_CLASS_NOT_AUTHORIZED": "This content is not approved for this destination.",
    "UNSUPPORTED_ACTION": "The available evidence does not support this action.",
    "KNOWN_PATTERN": "The action depends on content matching an accepted threat pattern.",
    "KNOWN_FAILURE": "This command would fail the same way it has failed here before (learned lesson).",
    "BUDGET_EXCEEDED": "The run exhausted an authorized budget.",
    "STALE_REVISION": "The task or policy changed since this run started; re-authorize first.",
    "OUTCOME_UNKNOWN": "A previous effect has an unknown outcome; reconcile before continuing.",
    "GRANT_REQUIRED": "This high-impact action needs a matching grant.",
    "SCHEMA_INVALID": "The proposal does not match the tool schema.",
    "TOOL_SCHEMA_MISMATCH": "The tool schema hash does not match the pinned registry entry.",
    "RUN_NOT_ACTIVE": "The run is not accepting new actions.",
    "OPERATION_CONFLICT": "This operation ID is bound to different arguments or ownership.",
    "OPERATION_NOT_ISSUED": "Operation IDs must be issued by the coordinator.",
    "AUDIT_UNCERTAIN": "The semantic audit was uncertain; the action is paused for review.",
    "AUDIT_FAILED": "The semantic auditor failed or timed out; mutating actions fail closed.",
    "POLICY_ERROR": "Policy evaluation failed; mutating actions fail closed.",
    "STORAGE_UNAVAILABLE": "Durable storage is unavailable; protection state is unknown.",
    "BARRIER_ACTIVE": "A save or restore barrier is active on this branch.",
    "SPENDING_LIMIT": "The amount exceeds the authorized spending limit.",
    "NETWORK_NOT_AUTHORIZED": "Network access for this request is not authorized.",
    "HARD_POLICY": "Operator hard policy forbids this action.",
    "LESSON_REVOKED": "Depends on a revoked lesson.",
    "DEDUPLICATED": "This logical operation already completed; returning the recorded result.",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- tasks
class Spending(Strict):
    currency: str = "USD"
    limit: str = "0.00"

    @field_validator("limit")
    @classmethod
    def _decimal(cls, v: str) -> str:
        try:
            d = Decimal(v)
        except InvalidOperation as e:  # pragma: no cover - validation path
            raise ValueError("limit must be a decimal string") from e
        if d < 0:
            raise ValueError("limit must be non-negative")
        return str(d.quantize(Decimal("0.01")))


class ReadNetwork(Strict):
    public_https: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)


class WriteNetwork(Strict):
    destination_ids: list[str] = Field(default_factory=list)


class TaskResources(Strict):
    source_ids: list[str] = Field(default_factory=list)
    output_path: Optional[str] = None
    urls: list[str] = Field(default_factory=list)
    required_phrases: list[str] = Field(default_factory=list)


class TaskBody(Strict):
    """Typed authority compiled by the trusted assignment service."""
    task_id: str
    goal: str
    tools: list[str]
    file_roots: list[str] = Field(default_factory=lambda: ["workspace://branch/current"])
    recipient_ids: list[str] = Field(default_factory=list)
    outbound_data_classes: list[str] = Field(default_factory=list)
    read_network: ReadNetwork = Field(default_factory=ReadNetwork)
    write_network: WriteNetwork = Field(default_factory=WriteNetwork)
    spending: Spending = Field(default_factory=Spending)
    grant_refs: list[str] = Field(default_factory=list)
    max_tool_steps: int = 30
    resources: TaskResources = Field(default_factory=TaskResources)


class TaskCreate(Strict):
    project_id: str
    task: TaskBody
    expected_revision: Optional[int] = None


# --------------------------------------------------------------------------- actions
class ActionProposal(Strict):
    """What a runtime submits. Identity and bindings come from server state."""
    action_id: str
    operation_id: str
    tool_id: str
    tool_schema_hash: Optional[str] = None
    arguments: dict[str, Any]
    observation_refs: list[str] = Field(default_factory=list)


class ModelAction(Strict):
    """The model-facing schema contains only tool selection and arguments."""
    tool_id: str
    arguments: dict[str, Any]


class DecisionView(BaseModel):
    decision_id: str
    action_id: str
    operation_id: str
    outcome: Outcome
    reason_code: str
    message: str
    arguments_hash: str
    policy_revision: str
    task_revision: int
    expires_at: str
    evidence_refs: list[str] = Field(default_factory=list)
    executor_invoked: bool = False
    attribution_status: str = "observed"
    result: Optional[dict] = None
    effect_state: Optional[str] = None
    deduplicated: bool = False


class AdapterCapabilities(BaseModel):
    intercept_actions: bool
    label_observations: bool
    export_messages: bool
    restore_cursor: bool
    isolate_workspace: bool
    cancel_or_pause: bool
    export_context: bool


# --------------------------------------------------------------------------- api bodies
class ProjectCreate(Strict):
    project_id: str
    name: str
    owner: str


class SourceImport(Strict):
    project_id: str
    channel: Literal["web", "document", "log", "calendar", "tool_result", "subagent"]
    filename: Optional[str] = None
    content_type: Optional[str] = None
    content_text: Optional[str] = None
    content_b64: Optional[str] = None
    url: Optional[str] = None
    label: Optional[str] = None


class RunStart(Strict):
    project_id: str
    agent_id: str
    task_ref: str
    base_version: Optional[str] = None
    branch_id: Optional[str] = None
    adapter: str = "langgraph_reference"
    context_budget_tokens: int = 6000
    mode: Optional[Mode] = None
    resume_run_id: Optional[str] = None
    wait: bool = False


class VersionSave(Strict):
    project_id: str
    branch_id: str
    expected_head: Optional[str] = None
    label: Optional[str] = None


class ForkRequest(Strict):
    project_id: str
    name: Optional[str] = None
    purpose: Literal["work", "restore", "repair", "experiment"] = "work"


class MergeRequest(Strict):
    project_id: str
    target_branch: str
    expected_head: Optional[str] = None
    source_version: str
    selected_changes: list[str]
    suite_revision: Optional[str] = None
    resolutions: dict[str, Literal["current", "candidate"]] = Field(default_factory=dict)


class EvaluateRequest(Strict):
    project_id: str
    suite_revision: Optional[str] = None
    wait: bool = False


class PromoteRequest(Strict):
    project_id: str
    expected_registry_revision: Optional[int] = None
    target_branch: Optional[str] = None
    expected_head: Optional[str] = None
    mode: Literal["assisted", "automatic"] = "assisted"


class RevokeRequest(Strict):
    project_id: str
    revision: Optional[int] = None
    reason: str


class GrantCreate(Strict):
    project_id: str
    tool_id: str
    arguments: Optional[dict[str, Any]] = None
    operation_class: Optional[str] = None
    resource_scope: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int = 3600
    uses: int = 1


class CandidateRevise(Strict):
    project_id: str
    conditions: Optional[dict[str, Any]] = None
    procedure: Optional[dict[str, Any]] = None
    negative_cases: Optional[list[str]] = None
    note: Optional[str] = None


class PreferenceSet(Strict):
    project_id: str
    scope: Literal["user", "agent", "project"]
    scope_id: str
    key: str
    value: Any


class MemoryEdit(Strict):
    project_id: str
    key: str
    value: Optional[Any] = None
    delete: bool = False
    note: Optional[str] = None
    seeded_test_condition: bool = False
