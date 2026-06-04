"""
Centralized LangGraph state definition for the Multi-Agent System.

MASState is the single source of truth that flows through every node in the
StateGraph.  Lists annotated with ``operator.add`` use LangGraph's reducer
pattern — individual agents append records without overwriting each other,
making concurrent execution safe without explicit locking.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict

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


class MASState(TypedDict):
    """
    Thread-safe, centralized state for the Multi-Agent System.

    LangGraph passes an immutable copy of state to each node and merges the
    returned partial dict back via reducers, so no two nodes share mutable
    references.

    Attributes:
        request_id:          UUID v4 for end-to-end distributed tracing.
        user_id:             Authenticated, tokenised user identifier.
        tenant_id:           Multi-tenant isolation key.
        session_id:          Conversational session for stateful turns.
        timestamp:           ISO-8601 request arrival time (UTC).
        raw_input:           Gateway-validated, PII-scrubbed payload.
        fetched_data:        Structured data returned by DataFetcher.
        validation_result:   Schema + business-rule check result.
        analysis_result:     Risk/fraud composite score and signals.
        supervisor_output:   Gemini-generated summary and recommendations.
        email_receipt:       Delivery confirmation from the Email agent.
        conversational_resp: Turn response from the Conversational agent.
        final_response:      Guardrail-checked, aggregated final output.
        current_agent:       Name of the agent currently executing.
        next_agent:          Optional routing override set by the orchestrator.
        execution_history:   Append-only audit log (reducer: operator.add).
        errors:              Append-only error records (reducer: operator.add).
        retry_counts:        Per-agent attempt counter for backoff logic.
        token_usage:         Token consumption keyed by agent name.
        execution_times:     Wall-clock duration (seconds) keyed by agent name.
        security_context:    JWT claims, mTLS fingerprint, RBAC roles.
        metadata:            Open key-value bag for pipeline extensions.
    """

    # ── Request metadata ──────────────────────────────────────────────────────
    request_id: str
    user_id: str
    tenant_id: str
    session_id: str
    timestamp: str

    # ── Data payloads ─────────────────────────────────────────────────────────
    raw_input: Dict[str, Any]
    fetched_data: Optional[FetchedData]
    validation_result: Optional[ValidationResult]
    analysis_result: Optional[AnalysisResult]
    supervisor_output: Optional[SupervisorOutput]
    email_receipt: Optional[EmailDeliveryReceipt]
    conversational_resp: Optional[ConversationalResponse]
    final_response: Optional[FinalResponse]

    # ── Routing & control ─────────────────────────────────────────────────────
    current_agent: str
    next_agent: Optional[str]

    # Append-only via LangGraph reducer — safe for concurrent graph execution
    execution_history: Annotated[List[AgentExecution], operator.add]
    errors: Annotated[List[AgentError], operator.add]

    retry_counts: Dict[str, int]

    # ── Observability ─────────────────────────────────────────────────────────
    token_usage: Dict[str, TokenUsage]
    execution_times: Dict[str, float]

    # ── Security ──────────────────────────────────────────────────────────────
    security_context: Optional[SecurityContext]

    # ── Extensions ────────────────────────────────────────────────────────────
    metadata: Dict[str, Any]


def initial_state(
    *,
    request_id: str,
    user_id: str,
    tenant_id: str,
    session_id: str,
    raw_input: Dict[str, Any],
    security_context: Optional[SecurityContext] = None,
    timestamp: Optional[str] = None,
) -> MASState:
    """
    Factory that creates a fully-initialised MASState for a new request.

    Args:
        request_id:       Caller-supplied UUID v4 correlation ID.
        user_id:          Tokenised user identifier from the gateway.
        tenant_id:        Tenant isolation key.
        session_id:       Conversational session ID.
        raw_input:        Scrubbed, validated request payload.
        security_context: Parsed JWT claims and mTLS metadata.
        timestamp:        ISO-8601 string; defaults to current UTC time.

    Returns:
        A MASState TypedDict with all optional fields set to None/empty
        and all required fields populated.
    """
    from datetime import datetime, timezone

    return MASState(
        request_id=request_id,
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        raw_input=raw_input,
        fetched_data=None,
        validation_result=None,
        analysis_result=None,
        supervisor_output=None,
        email_receipt=None,
        conversational_resp=None,
        final_response=None,
        current_agent="orchestrator",
        next_agent=None,
        execution_history=[],
        errors=[],
        retry_counts={},
        token_usage={},
        execution_times={},
        security_context=security_context,
        metadata={},
    )
