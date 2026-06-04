"""
Pydantic v2 data models for every agent's input and output contracts.

Every agent boundary is typed.  A schema mismatch raises a ValidationError
before any downstream agent executes, preventing cascading failures.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class AgentStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    RETRYING = "retrying"
    SKIPPED = "skipped"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ValidationSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EmailPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# ─────────────────────────────────────────────────────────────────────────────
# DataFetcher (Agent A) — AWS
# ─────────────────────────────────────────────────────────────────────────────


class DataFetcherInput(BaseModel):
    """Input contract for the DataFetcher agent."""

    user_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$")
    tenant_id: str = Field(..., min_length=1, max_length=64)
    fetch_types: List[str] = Field(
        default=["profile", "transactions", "kyc"],
        description="Data categories to retrieve from upstream APIs.",
    )
    date_range_days: int = Field(default=90, ge=1, le=365)


class Transaction(BaseModel):
    transaction_id: str
    amount: float
    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    merchant_category: str
    timestamp: datetime
    country_code: str = Field(..., pattern=r"^[A-Z]{2}$")
    is_card_present: bool
    channel: str


class UserProfile(BaseModel):
    user_id: str
    full_name: str
    email: str
    kyc_status: str
    account_age_days: int
    country_of_residence: str
    risk_band: str


class FetchedData(BaseModel):
    """Structured output from the DataFetcher agent."""

    user_id: str
    profile: UserProfile
    transactions: List[Transaction]
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    source_system: str = "aws-dynamodb"

    @field_validator("transactions")
    @classmethod
    def transactions_not_empty(cls, v: List[Transaction]) -> List[Transaction]:
        """At least one transaction is required for meaningful analysis."""
        if len(v) == 0:
            raise ValueError("transactions list must not be empty")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# DataValidator (Agent B) — On-Prem
# ─────────────────────────────────────────────────────────────────────────────


class ValidationIssue(BaseModel):
    field: str
    rule: str
    severity: ValidationSeverity
    message: str
    observed_value: Optional[str] = None


class ValidationResult(BaseModel):
    """Output contract for the DataValidator agent."""

    is_valid: bool
    issues: List[ValidationIssue] = Field(default_factory=list)
    validated_at: datetime = Field(default_factory=datetime.utcnow)
    schema_version: str = "1.0"
    rules_applied: List[str] = Field(default_factory=list)
    transaction_count: int = 0
    critical_issue_count: int = 0

    @model_validator(mode="after")
    def sync_critical_count(self) -> "ValidationResult":
        self.critical_issue_count = sum(
            1 for i in self.issues if i.severity == ValidationSeverity.CRITICAL
        )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Analyst (Agent A) — AWS
# ─────────────────────────────────────────────────────────────────────────────


class FraudSignal(BaseModel):
    signal_name: str
    score: float = Field(..., ge=0.0, le=1.0)
    weight: float = Field(..., ge=0.0, le=1.0)
    evidence: str


class BacktestMetric(BaseModel):
    metric_name: str
    value: float
    benchmark: float
    delta: float
    passed: bool


class AnalysisResult(BaseModel):
    """Output contract for the Analyst agent."""

    user_id: str
    composite_risk_score: float = Field(..., ge=0.0, le=100.0)
    risk_level: RiskLevel
    fraud_signals: List[FraudSignal] = Field(default_factory=list)
    backtest_metrics: List[BacktestMetric] = Field(default_factory=list)
    recommended_action: str
    requires_human_review: bool
    analysed_at: datetime = Field(default_factory=datetime.utcnow)
    model_version: str
    explanation: str

    @field_validator("composite_risk_score")
    @classmethod
    def score_precision(cls, v: float) -> float:
        return round(v, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor (Agent C) — On-Prem Gemini
# ─────────────────────────────────────────────────────────────────────────────


class SupervisorInput(BaseModel):
    """Input assembled by the orchestrator for the Supervisor agent."""

    user_id: str
    tenant_id: str
    fetched_data: FetchedData
    validation_result: ValidationResult
    analysis_result: Optional[AnalysisResult] = None


class StrategicRecommendation(BaseModel):
    priority: int = Field(..., ge=1, le=5)
    action: str
    rationale: str
    owner: str
    timeline: str


class SupervisorOutput(BaseModel):
    """Output contract for the Supervisor (non-deterministic) agent."""

    user_id: str
    executive_summary: str = Field(..., min_length=50, max_length=2000)
    strategic_recommendations: List[StrategicRecommendation]
    risk_narrative: str
    next_steps: List[str]
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    model_id: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    guardrail_passed: bool = False

    @field_validator("strategic_recommendations")
    @classmethod
    def at_least_one_recommendation(
        cls, v: List[StrategicRecommendation]
    ) -> List[StrategicRecommendation]:
        if len(v) == 0:
            raise ValueError("supervisor must provide at least one recommendation")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Email Agent (Copilot) — Azure
# ─────────────────────────────────────────────────────────────────────────────


class EmailPayload(BaseModel):
    """Input for the Email agent."""

    recipient_email: str
    recipient_name: str
    subject: str
    html_body: str
    plain_text_body: str
    priority: EmailPriority = EmailPriority.NORMAL
    attachments: List[Dict[str, str]] = Field(default_factory=list)


class EmailDeliveryReceipt(BaseModel):
    """Output from the Email agent."""

    message_id: str
    recipient_email: str
    status: str
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    provider: str = "azure-communication-services"
    error_detail: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Conversational Agent — GCP
# ─────────────────────────────────────────────────────────────────────────────


class ConversationalInput(BaseModel):
    """Input for the Conversational Agent."""

    session_id: str
    user_utterance: str
    context: Dict[str, Any] = Field(default_factory=dict)
    language_code: str = "en-US"


class ConversationalResponse(BaseModel):
    """Output from the Conversational Agent."""

    session_id: str
    fulfillment_text: str
    intent_detected: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    end_interaction: bool = False
    responded_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Aggregated Final Response
# ─────────────────────────────────────────────────────────────────────────────


class FinalResponse(BaseModel):
    """Aggregated, guardrail-checked response returned to the caller."""

    request_id: str
    user_id: str
    status: AgentStatus
    risk_level: Optional[RiskLevel] = None
    executive_summary: Optional[str] = None
    recommendations: List[str] = Field(default_factory=list)
    email_sent: bool = False
    conversation_active: bool = False
    errors: List[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=datetime.utcnow)
    pipeline_duration_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Observability Models
# ─────────────────────────────────────────────────────────────────────────────


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_id: str = ""
    cost_usd: float = 0.0


class AgentExecution(BaseModel):
    """Immutable audit record appended to execution_history on each transition."""

    agent_name: str
    status: AgentStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    input_summary: Dict[str, Any] = Field(default_factory=dict)
    output_summary: Dict[str, Any] = Field(default_factory=dict)
    retry_attempt: int = 0
    platform: str = "unknown"


class AgentError(BaseModel):
    """Structured error record appended to errors list."""

    agent_name: str
    error_type: str
    message: str
    traceback: Optional[str] = None
    occurred_at: datetime = Field(default_factory=datetime.utcnow)
    retry_attempt: int = 0
    is_fatal: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Security Models
# ─────────────────────────────────────────────────────────────────────────────


class SecurityContext(BaseModel):
    """JWT claims and mTLS metadata attached to every request."""

    subject: str
    tenant_id: str
    roles: List[str] = Field(default_factory=list)
    scopes: List[str] = Field(default_factory=list)
    issuer: str
    issued_at: datetime
    expires_at: datetime
    mtls_fingerprint: Optional[str] = None
    ip_address: Optional[str] = None
    device_id: Optional[str] = None
