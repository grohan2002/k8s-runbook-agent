"""Pydantic models shared across the K8s Runbook Agent."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Grafana alert payload
# ---------------------------------------------------------------------------
class AlertStatus(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"


class GrafanaAlert(BaseModel):
    """Parsed payload from a Grafana webhook notification."""

    alert_name: str
    status: AlertStatus
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime | None = None
    generator_url: str | None = None
    fingerprint: str | None = None

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "default")

    @property
    def pod(self) -> str | None:
        return self.labels.get("pod") or self.labels.get("pod_name")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "warning")

    @property
    def summary(self) -> str:
        return self.annotations.get("summary", self.alert_name)


# ---------------------------------------------------------------------------
# Diagnostic runbook (loaded from YAML)
# ---------------------------------------------------------------------------
class InspectionStep(BaseModel):
    """One step of the initial investigation."""

    tool: str
    why: str
    args: dict[str, Any] = Field(default_factory=dict)


class RootCause(BaseModel):
    cause: str
    confidence_signals: list[str] = Field(default_factory=list)
    resolution_strategy: str = ""


class DiagnosisBranch(BaseModel):
    """One symptom branch in the diagnosis tree."""

    symptom: str
    investigation: list[str] = Field(default_factory=list)
    root_causes: list[RootCause] = Field(default_factory=list)


class Fallback(BaseModel):
    message: str = "Unable to determine root cause from automated inspection."
    action: str = "Collect diagnostic bundle and escalate."
    collect: list[str] = Field(default_factory=list)


class RunbookMetadata(BaseModel):
    id: str
    title: str
    description: str = ""
    severity_signals: list[dict[str, str]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class DiagnosticRunbook(BaseModel):
    """A full diagnostic runbook loaded from YAML."""

    metadata: RunbookMetadata
    initial_inspection: list[InspectionStep] = Field(default_factory=list)
    diagnosis_tree: list[DiagnosisBranch] = Field(default_factory=list)
    fallback: Fallback = Field(default_factory=Fallback)


# ---------------------------------------------------------------------------
# Agent session state
# ---------------------------------------------------------------------------
class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Diagnosis(BaseModel):
    """Agent's root-cause assessment after investigation."""

    root_cause: str
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)


class FixProposal(BaseModel):
    """A proposed remediation constructed by the agent."""

    summary: str
    description: str
    risk_level: RiskLevel
    dry_run_output: str = ""
    rollback_plan: str = ""
    requires_human_values: bool = False
    human_value_fields: list[str] = Field(default_factory=list)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalState(BaseModel):
    """Tracks human approval for a proposed fix."""

    incident_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: str | None = None
    approved_at: datetime | None = None
    fix_proposal: FixProposal | None = None
    pre_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    executed: bool = False
    execution_result: str | None = None


# ---------------------------------------------------------------------------
# Runbook search result
# ---------------------------------------------------------------------------
class RunbookMatch(BaseModel):
    """A runbook matched to an alert with a relevance score."""

    runbook_id: str
    title: str
    score: float
    match_reasons: list[str] = Field(default_factory=list)
