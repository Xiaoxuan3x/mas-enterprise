"""
Central Orchestrator — Platform: On-Prem (NVIDIA GPU server)

Manages state initialisation, task routing, and pipeline coordination.
Implements routing logic that determines the next agent based on current
state, handles conditional branching, and aggregates the final response.

The orchestrator is a pure routing/coordination component: it contains no
business logic, no LLM calls, and no data transformations.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    FinalResponse,
    RiskLevel,
)
from schemas.state import MASState
from core.logging_config import agent_span, get_logger

logger = get_logger(__name__)

AGENT_NAME = "orchestrator"
PLATFORM = "on-prem"


# ─────────────────────────────────────────────────────────────────────────────
# Routing functions (used as LangGraph conditional edge callbacks)
# ─────────────────────────────────────────────────────────────────────────────


def route_after_validation(
    state: MASState,
) -> Literal["analyst", "supervisor", "error_handler"]:
    """
    Route after the DataValidator node.

    Logic:
      - If critical validation errors → route directly to supervisor for
        graceful error summarisation (skip analyst).
      - If data is valid or has only warnings → route to analyst.
      - If fetched_data is missing entirely → route to error handler.

    Args:
        state: Current MASState after DataValidator has run.

    Returns:
        Node name string for the next LangGraph edge.
    """
    if state.get("fetched_data") is None:
        logger.warning("orchestrator.route", decision="error_handler", reason="no_fetched_data")
        return "error_handler"

    validation = state.get("validation_result")
    if validation is None:
        logger.warning("orchestrator.route", decision="error_handler", reason="no_validation")
        return "error_handler"

    if validation.critical_issue_count > 0:
        logger.info(
            "orchestrator.route",
            decision="supervisor",
            reason="critical_validation_issues",
            critical_count=validation.critical_issue_count,
        )
        return "supervisor"

    logger.info("orchestrator.route", decision="analyst", reason="validation_passed")
    return "analyst"


def route_after_supervisor(
    state: MASState,
) -> Literal["email_agent", "conversational_agent", "finalize"]:
    """
    Route after the Supervisor node.

    Logic:
      - If raw_input requests an email notification → route to email_agent.
      - If raw_input contains an utterance for conversation → route to conversational_agent.
      - Otherwise → finalize directly.

    Args:
        state: Current MASState after Supervisor has run.

    Returns:
        Node name string for the next LangGraph edge.
    """
    raw = state.get("raw_input", {})

    if raw.get("send_email", False) or raw.get("notification_email"):
        logger.info("orchestrator.route", decision="email_agent")
        return "email_agent"

    if raw.get("utterance"):
        logger.info("orchestrator.route", decision="conversational_agent")
        return "conversational_agent"

    logger.info("orchestrator.route", decision="finalize")
    return "finalize"


def route_after_email(
    state: MASState,
) -> Literal["conversational_agent", "finalize"]:
    """
    Route after the Email Agent node.

    If the request also contains a conversational utterance, continue to
    the Conversational Agent; otherwise finalize.

    Args:
        state: Current MASState after Email Agent has run.

    Returns:
        Node name string.
    """
    if state.get("raw_input", {}).get("utterance"):
        return "conversational_agent"
    return "finalize"


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator node (initialisation / entry)
# ─────────────────────────────────────────────────────────────────────────────


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph entry node for the orchestrator.

    Validates that required state fields are present, sets routing metadata,
    and emits an execution record.  Does not modify data payloads.

    Args:
        state: Current MASState from the initial_state factory.

    Returns:
        Partial MASState dict updating ``current_agent`` and appending an
        ``AgentExecution`` record to ``execution_history``.
    """
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        span["user_id"] = state["user_id"]
        span["tenant_id"] = state["tenant_id"]

        duration_ms = (time.perf_counter() - start_time) * 1000

        return {
            "current_agent": AGENT_NAME,
            "execution_history": [
                AgentExecution(
                    agent_name=AGENT_NAME,
                    status=AgentStatus.SUCCESS,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    duration_ms=round(duration_ms, 2),
                    input_summary={
                        "user_id": state["user_id"],
                        "tenant_id": state["tenant_id"],
                        "request_id": state["request_id"],
                    },
                    platform=PLATFORM,
                )
            ],
            "errors": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Error handler node
# ─────────────────────────────────────────────────────────────────────────────


async def error_handler(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node for terminal error handling.

    Invoked when a fatal upstream failure prevents the pipeline from
    continuing meaningfully (e.g., fetched_data is missing).  Constructs a
    graceful FinalResponse indicating the failure without exposing internals.

    Args:
        state: Current MASState with error records populated.

    Returns:
        Partial MASState dict with ``final_response`` set to a failure record.
    """
    started_at = datetime.now(timezone.utc)
    error_messages = [e.message for e in state.get("errors", [])]

    final = FinalResponse(
        request_id=state["request_id"],
        user_id=state["user_id"],
        status=AgentStatus.FAILURE,
        risk_level=None,
        executive_summary=None,
        recommendations=[],
        email_sent=False,
        conversation_active=False,
        errors=error_messages[:5],
        pipeline_duration_ms=_pipeline_duration_ms(state),
    )

    logger.error(
        "orchestrator.pipeline_failed",
        request_id=state["request_id"],
        error_count=len(error_messages),
    )

    return {
        "final_response": final,
        "current_agent": "error_handler",
        "execution_history": [
            AgentExecution(
                agent_name="error_handler",
                status=AgentStatus.FAILURE,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                duration_ms=0,
                platform=PLATFORM,
            )
        ],
        "errors": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Finalize node
# ─────────────────────────────────────────────────────────────────────────────


async def finalize(state: MASState) -> Dict[str, Any]:
    """
    LangGraph terminal node that assembles the FinalResponse from all agent
    outputs collected in state.

    Args:
        state: Fully-populated MASState after all upstream agents have run.

    Returns:
        Partial MASState dict with ``final_response`` set to the aggregated
        pipeline result.
    """
    started_at = datetime.now(timezone.utc)

    supervisor = state.get("supervisor_output")
    analysis = state.get("analysis_result")
    email = state.get("email_receipt")
    conversation = state.get("conversational_resp")

    recommendations: list[str] = []
    if supervisor:
        recommendations = [r.action for r in supervisor.strategic_recommendations]

    email_sent = (
        email is not None and email.status not in ("Failed", "FAILED")
    )
    conversation_active = (
        conversation is not None and not conversation.end_interaction
    )

    final = FinalResponse(
        request_id=state["request_id"],
        user_id=state["user_id"],
        status=AgentStatus.SUCCESS,
        risk_level=analysis.risk_level if analysis else None,
        executive_summary=supervisor.executive_summary if supervisor else None,
        recommendations=recommendations,
        email_sent=email_sent,
        conversation_active=conversation_active,
        errors=[e.message for e in state.get("errors", [])],
        pipeline_duration_ms=_pipeline_duration_ms(state),
    )

    logger.info(
        "orchestrator.pipeline_complete",
        request_id=state["request_id"],
        risk_level=final.risk_level.value if final.risk_level else "unknown",
        email_sent=email_sent,
        duration_ms=final.pipeline_duration_ms,
    )

    return {
        "final_response": final,
        "current_agent": "finalize",
        "execution_history": [
            AgentExecution(
                agent_name="finalize",
                status=AgentStatus.SUCCESS,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                duration_ms=0,
                platform=PLATFORM,
            )
        ],
        "errors": [],
    }


def _pipeline_duration_ms(state: MASState) -> float:
    """
    Compute total pipeline wall-clock duration from execution_times in state.

    Args:
        state: Current MASState with execution_times dict.

    Returns:
        Sum of all recorded agent durations in milliseconds.
    """
    return round(sum(state.get("execution_times", {}).values()) * 1000, 2)
