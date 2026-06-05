"""
Pydantic contracts for distributed agent execution over HTTP.

These models let the on-prem orchestrator send a validated snapshot of the
central MAS state to a remote agent service and receive back a validated state
patch. The patch is then merged into the orchestrator-owned state object.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AnalysisResult,
    ConversationalResponse,
    EmailDeliveryReceipt,
    FetchedData,
    FinalResponse,
    SecurityContext,
    SupervisorOutput,
    TokenUsage,
    ValidationResult,
)


class StateSnapshot(BaseModel):
    """
    Serializable representation of MASState used for inter-service transport.

    The orchestrator remains the single owner of workflow state. For each
    remote step it sends a validated snapshot, then merges the returned patch.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    user_id: str
    tenant_id: str
    session_id: str
    timestamp: str

    raw_input: Dict[str, Any]
    fetched_data: Optional[FetchedData] = None
    validation_result: Optional[ValidationResult] = None
    analysis_result: Optional[AnalysisResult] = None
    supervisor_output: Optional[SupervisorOutput] = None
    email_receipt: Optional[EmailDeliveryReceipt] = None
    conversational_resp: Optional[ConversationalResponse] = None
    final_response: Optional[FinalResponse] = None

    current_agent: str
    next_agent: Optional[str] = None
    execution_history: List[AgentExecution] = Field(default_factory=list)
    errors: List[AgentError] = Field(default_factory=list)
    retry_counts: Dict[str, int] = Field(default_factory=dict)
    token_usage: Dict[str, TokenUsage] = Field(default_factory=dict)
    execution_times: Dict[str, float] = Field(default_factory=dict)
    security_context: Optional[SecurityContext] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "StateSnapshot":
        """Validate a plain MAS state dict before sending it to a service."""
        return cls.model_validate(state)

    def to_state(self) -> Dict[str, Any]:
        """Rehydrate a transport snapshot into a state dict with model objects."""
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "raw_input": dict(self.raw_input),
            "fetched_data": self.fetched_data,
            "validation_result": self.validation_result,
            "analysis_result": self.analysis_result,
            "supervisor_output": self.supervisor_output,
            "email_receipt": self.email_receipt,
            "conversational_resp": self.conversational_resp,
            "final_response": self.final_response,
            "current_agent": self.current_agent,
            "next_agent": self.next_agent,
            "execution_history": list(self.execution_history),
            "errors": list(self.errors),
            "retry_counts": dict(self.retry_counts),
            "token_usage": dict(self.token_usage),
            "execution_times": dict(self.execution_times),
            "security_context": self.security_context,
            "metadata": dict(self.metadata),
        }


class StatePatch(BaseModel):
    """
    Partial state update returned by a remote agent service.

    Every field is optional so each service can return only the values it owns.
    """

    model_config = ConfigDict(extra="forbid")

    raw_input: Optional[Dict[str, Any]] = None
    fetched_data: Optional[FetchedData] = None
    validation_result: Optional[ValidationResult] = None
    analysis_result: Optional[AnalysisResult] = None
    supervisor_output: Optional[SupervisorOutput] = None
    email_receipt: Optional[EmailDeliveryReceipt] = None
    conversational_resp: Optional[ConversationalResponse] = None
    final_response: Optional[FinalResponse] = None
    current_agent: Optional[str] = None
    next_agent: Optional[str] = None
    execution_history: Optional[List[AgentExecution]] = None
    errors: Optional[List[AgentError]] = None
    retry_counts: Optional[Dict[str, int]] = None
    token_usage: Optional[Dict[str, TokenUsage]] = None
    execution_times: Optional[Dict[str, float]] = None
    security_context: Optional[SecurityContext] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_patch(self) -> Dict[str, Any]:
        """Return only fields actually set by the remote service."""
        patch: Dict[str, Any] = {}
        for field_name in self.__class__.model_fields:
            value = getattr(self, field_name)
            if value is not None:
                patch[field_name] = value
        return patch


class AgentServiceRequest(BaseModel):
    """HTTP request body sent from the orchestrator to a remote agent."""

    state: StateSnapshot


class AgentServiceResponse(BaseModel):
    """HTTP response body containing the remote agent's validated state patch."""

    agent_name: str
    state_patch: StatePatch
